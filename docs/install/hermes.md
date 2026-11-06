# Hermes

## Plugin catalog

For a published Scope Recall entry in the Hermes plugin catalog:

```sh
hermes plugins install scope-recall
hermes memory setup
```

Select `scope-recall`, then start a new session. The provider's setup hook uses the same local setup workflow as
`scope-recall setup --host hermes`: it creates `~/.scope-recall/shared` or reuses the home's existing attachment,
installs the memory skill, and selects `memory.provider`. New installations use original-message retrieval with
model routes disabled and a zero budget. Setup preserves the other configuration bytes and leaves the
catalog-installed plugin directory under Hermes's control. Unsupported YAML layouts are refused with a reason.

## Package installation

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

After an idle period, binding a session warms the query embedding route in the background. This request counts toward the auxiliary budget; private mode blocks the warm-up.

## Model credentials

Model keys belong in this Hermes profile's own `.env` or in a secret source managed by Hermes. The in-process
plugin resolves each route's `credential_env` through `agent.secret_scope.get_secret`, which honours the active
profile. It does not read `.env` or other credential files itself. The plugin adds only the declared route keys
to each background worker's inherited process environment. Missing keys pause the affected work types; later
passes recheck missing names at most once every 60 seconds. Each missing name is logged once per process.

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
