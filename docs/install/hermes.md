# Hermes

1. Install the package in the intended Python environment: `python -m pip install -U "hermes-scope-recall[codex]"`.
2. Run `scope-recall setup --host hermes`, review its plan and confirm.
3. Start a new host session and complete any approval shown by the host.

Discovery uses `HERMES_HOME`, otherwise `~/.hermes`. A host-local venv is preferred; setup shows the exact interpreter
and whether it fell back to its own Python. Use `--home` or `--python` only to override that choice.

Setup selects memory.provider: scope-recall in config.yaml and installs the provider and memory skill. The CLI owner identity is the default; desktop, TUI and remote login audiences still require explicit owner authorization.

A scheduled session (`platform=cron`) with no user id does not participate in memory. Its hooks return without capture,
recall or background work, and diagnostics report `capability_gap:unbound_session`. The reason is logged once at debug
level, not as a failure on every hook. No installation owner or earlier session's user is substituted; other identity
and installation failures still raise their normal errors.

## Package-managed plugin environments

Some Hermes installations build a separate environment from the declarations in the home's `plugins` directory.
Updating the interpreter that runs the `hermes` command does not update that environment. Run
`scope-recall doctor --host hermes --instance-root /path/to/hermes-home` and inspect `hermes_runtime` for its
recorded environment, package version and NumPy availability. An unreadable environment remains unverified.

After stopping the gateway and workers and backing up their configuration and store, refresh the wrapper with
`plan-install` and `apply-install`, selecting the home's `plugins/scope-recall` directory. Rebuild through Hermes's
own environment sync. If the pinned version is unavailable from the package index, keep a directory containing
its exact wheel available as `UV_FIND_LINKS` during every rebuild, including after `hermes update`. Keep that
wheel directory. For example, from a POSIX shell:

```sh
HERMES_HOME=/path/to/hermes-home UV_FIND_LINKS=/path/to/wheels /path/to/hermes-python -m hermes_cli.venv_sync --project-root /path/to/hermes-source --json
```

Use the actual Hermes interpreter and source directory. On Windows, set `HERMES_HOME` and `UV_FIND_LINKS` in the
invoking process and run the same Python module and arguments from PowerShell.
Then restart through the host controller and verify doctor and a real host recall. The
[package-managed upgrade procedure](../../maintenance/AGENT_WORKFLOW.md#12-a-hermes-that-builds-its-own-plugin-environment)
gives the sync command and failure recovery steps.

[Common installation options, models, worker modes and verification](../install.md) apply to this host.
