# Hermes

1. Install the package in the intended Python environment: `python -m pip install "hermes-scope-recall[codex]"`.
2. Run `scope-recall setup --host hermes`, review its plan and confirm.
3. Start a new host session and complete any approval shown by the host.

Discovery uses `HERMES_HOME`, otherwise `~/.hermes`. A host-local venv is preferred; setup shows the exact interpreter
and whether it fell back to its own Python. Use `--home` or `--python` only to override that choice.

Setup selects memory.provider: scope-recall in config.yaml and installs the provider and memory skill. The CLI owner identity is the default; desktop, TUI and remote login audiences still require explicit owner authorization.

[Common installation options, models, worker modes and verification](../install.md) apply to this host.
