# Codex

1. Install the package in the intended Python environment: `python -m pip install "hermes-scope-recall[codex]"`.
2. Run `scope-recall setup --host codex`, review its plan and confirm.
3. Start a new host session and complete any approval shown by the host.

Discovery uses `CODEX_HOME`, otherwise `~/.codex`. A host-local venv is preferred; setup shows the exact interpreter
and whether it fell back to its own Python. Use `--home` or `--python` only to override that choice.

Enable the local plugin in the Plugins page and review its hooks in /hooks. Setup never fabricates hook trust. MCP wiring belongs to the plugin; do not add a second server.

[Common installation options, models, worker modes and verification](../install.md) apply to this host.
