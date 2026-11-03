# DeepSeek Harness

1. Install the package in the intended Python environment: `python -m pip install -U "hermes-scope-recall[codex]"`.
2. Run `scope-recall setup --host dsh`, review its plan and confirm.
3. Start a new host session and complete any approval shown by the host.

Discovery uses `DSH_HOME`, otherwise `~/.dsh`. A host-local venv is preferred; setup shows the exact interpreter
and whether it fell back to its own Python. Use `--home` or `--python` only to override that choice.

Close active dsh profiles before changing their patch, then reopen them. The native plugin runs capture and recall through its hook bridge. Check `dsh --profile headless --dump-config` for `scope-recall` and `mcp-scope-recall` and for disabled `session-log-deepseek` upload. Keep the dump private because remote MCP settings can contain a token. The memory skill is installed under `$DSH_HOME/skills/scope-recall-memory/SKILL.md`, which the filesystem skill provider scans.

[Common installation options, models, worker modes and verification](../install.md) apply to this host.
