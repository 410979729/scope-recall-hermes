# Installing Scope Recall

Scope Recall is a bounded local memory core for coding agents: SQLite holds the
truth, vector indexes are rebuildable companions, and a bounded background worker
does the consolidating and embedding. It ships host adapters for Hermes and for
Codex, the latter as a set of native hooks plus an MCP server.

> **Status.** This guide is for the 3.1 line. Releases are on PyPI and on the
> GitHub releases page; a checkout between releases carries a candidate version
> and is installed by building its wheel. The distribution name is
> `hermes-scope-recall`, the Python import is `scope_recall`, and the host plugin
> identity is `scope-recall`.

v3 has no automatic `update` / `upgrade` / `rollback` commands. Moving data from an
older database is a separate, explicit operation — see
[upgrade-guide.zh-CN.md](upgrade-guide.zh-CN.md).

## 1. Requirements

- **Python 3.11 or 3.12.** `pyproject.toml` declares `requires-python = ">=3.11,<3.13"`;
  3.13 and newer are not supported.
- Install into **the same isolated Python environment the host uses**. Host
  discovery goes through that environment's package metadata.
- Runtime dependencies are small and pure-Python: `PyYAML`, `jsonschema`,
  `packaging`, and `tzdata` on Windows only.

Two optional extras:

| Extra | Adds | What it enables |
|-------|------|-----------------|
| `lancedb` | `lancedb`, `pyarrow` | The LanceDB vector companion, which is the `vector.backend` default. Without it, use `sqlite-bruteforce`, which needs no extra. |
| `codex` | `mcp`, `pydantic` | The Codex MCP server. Without it, the Core and Hermes paths still import and the Codex hooks still run, but the MCP server cannot start. |

A third extra, `dev`, adds `build`, `pytest`, `ruff`, `pyright` and packaging
tools. You need `build` (or the `dev` extra) to produce the wheel.

## 2. Install the package

From PyPI, into the host's environment:

```text
python -m pip install "hermes-scope-recall[lancedb]"
```

Or build the wheel from a source tree and install that file, which is how a
candidate between releases is installed. The file name carries the version of the
tree you built; the ones below are examples.

### Windows

```powershell
cd C:\path\to\scope-recall-source
py -m pip install build
py -m build --wheel
py -m pip install "C:\path\to\scope-recall-source\dist\hermes_scope_recall-<version>-py3-none-any.whl[lancedb]"
```

### Linux and macOS

```bash
cd /path/to/scope-recall-source
python3 -m pip install build
python3 -m build --wheel
python3 -m pip install "/path/to/scope-recall-source/dist/hermes_scope_recall-<version>-py3-none-any.whl[lancedb]"
```

Quote the whole argument: the `[extra]` suffix is shell metacharacters in both
shells, and the path may contain spaces. To install both extras, write
`...whl[lancedb,codex]`.

Two console entry points are installed, and they are the same program:

```powershell
scope-recall --help
hermes-scope-recall --help
```

They are aliases for the current v3 maintenance CLI only. Neither promises
compatibility with an older command set.

For Hermes, the wheel also declares an entry point: group
`hermes_agent.memory_providers`, name `scope-recall`, target
`scope_recall.distribution.hermes:register`. **Host discovery uses that entry
point of the installed package.** Do not try to fake discovery by copying or
symlinking a directory into the host's plugin folder.

## 3. Three states, kept separate

| State | Who does it | Done when |
|-------|-------------|-----------|
| **Installed** | `plan-install` then `apply-install` | The wrapper files and the install receipt exist |
| **Enabled in the host** | you, in the host's own configuration | The host actually loads the plugin and the memory tools work |
| **Hooks trusted** (Codex only) | you, in Codex | Codex is willing to run the commands in `hooks/hooks.json` |

`apply-install` does the first row and nothing else. It does not edit the host's
own configuration, register the plugin for you, or approve hooks. The receipt at
`<instance-root>\.scope-recall-install-receipt.json` records the state **at
install time**: `host_registration_pending: true`, plus `hook_trust_pending: true`
for Codex. Later enabling or trusting does not rewrite that historical receipt.
For the current state, read `doctor` and the host's actual behaviour.

Installation mode is an explicit boundary. Both `plan-install` and `apply-install`
create a production binding (`test_mode=false`) by default. Only an isolated TEST
root should carry `--test-mode`, and it must be passed to **both** commands;
`apply-install` re-plans and re-checks the mode before it initialises anything.

There is no single `install` command. There is an agent-facing router,
`scope-recall setup --host <hermes|codex> --home <instance-home>`, which inspects
a directory and reports whether it needs a fresh install, an ordinary update, or a
legacy migration; `scope-recall setup --workflow` prints the bundled workflow.
The commands below are the install itself.

## 4. Install for a Hermes host

All paths must be absolute.

`--instance-root` may be an **existing** Hermes home. The installer manages only
the `scope-recall\` namespace and the receipt inside it; it does not treat
`config.yaml`, sessions or other plugins as foreign. It will refuse an existing
`scope-recall\` directory that no receipt explains, and it never adopts an unknown
managed directory.

`--target-plugin-dir` must **not** sit inside `--instance-root` — the three roots
may not overlap in either direction. So do not use the host's own
`plugins\scope-recall` path as the wrapper target; put the wrapper outside the
home. Discovery comes from the entry point, not from that location. The directory
name itself must match `^[a-z][a-z0-9-]*$`.

`--agent-id` must equal the `agent_identity` the host sends on `initialize`: the
adapter compares them and raises `agent_identity conflict` when they differ
(`adapters/hermes/identity.py`). `--agent-workspace` defaults to `hermes`, which
is the value the host's memory-provider init contract uses; override it only if
your host really sends something else, and then pass the same value to plan and
apply. A mismatch still installs, but capture is refused later because the
audience cannot be mapped.

TODO(verify): which identity string your host actually sends is decided by the
host's own active-profile lookup, which is not in this source tree. Read it from
the host rather than guessing; on an isolated home it is commonly `default`, but
this repository cannot confirm that.

```powershell
$Instance = "C:\path\to\hermes-home"
$Plugin   = "C:\path\to\wrappers\scope-recall"
$Project  = "C:\path\to\your\repo"
$Python   = "C:\path\to\python.exe"

scope-recall plan-install --host hermes `
  --target-plugin-dir $Plugin --instance-root $Instance `
  --project-root $Project --agent-id default --python $Python

scope-recall apply-install --host hermes `
  --target-plugin-dir $Plugin --instance-root $Instance `
  --project-root $Project --agent-id default --python $Python
```

```bash
INSTANCE=/path/to/hermes-home
PLUGIN=/path/to/wrappers/scope-recall
PROJECT=/path/to/your/repo
PYTHON=/path/to/python

scope-recall plan-install --host hermes \
  --target-plugin-dir "$PLUGIN" --instance-root "$INSTANCE" \
  --project-root "$PROJECT" --agent-id default --python "$PYTHON"

scope-recall apply-install --host hermes \
  --target-plugin-dir "$PLUGIN" --instance-root "$INSTANCE" \
  --project-root "$PROJECT" --agent-id default --python "$PYTHON"
```

- `plan-install` prints JSON. When `conflicts` is non-empty it **exits 1**; resolve
  the conflicts before applying.
- `apply-install` exits 0 and prints `files_written`, `installation_id`,
  `receipt_path` and `backups`. Files it overwrites are copied first into
  `<instance-root>\.scope-recall-backups\`.
- Both the plan and the receipt carry `agent_workspace`. Codex rejects that flag.
- `--env-file` is refused for `--host hermes`: Hermes processes inherit the
  gateway environment.

#### Hermes Desktop and `hermes --tui`: `--local-platform`

A fresh install gives the owner's private memory to one surface, the CLI. Hermes
Desktop's chat panel and `hermes --tui` reach the adapter as platform `desktop`
and `tui`. The host passes a dashboard login as `user_id` there, and passes
nothing when nobody logged in, which is the ordinary case for a local profile.
A session that names no user is refused everywhere but the CLI:

```
Memory provider 'scope-recall' initialize failed: user principal required for non-cli platform; approve it as the owner's local surface with apply-install --local-platform desktop
```

Approve the surface when you install, or later on the same instance with the same
other arguments. The flag is repeatable and takes `desktop` or `tui`:

```powershell
scope-recall apply-install --host hermes `
  --target-plugin-dir $Plugin --instance-root $Instance `
  --project-root $Project --agent-id default --python $Python `
  --local-platform desktop
```

What it does: it adds the owner principal `(desktop, local)` and one grant of the
owner's private scope on that route to `installation.json`, keeping a copy of the
previous file under `.scope-recall-backups\`. The scopes, the installation id and
`memory.sqlite3` are unchanged, so Desktop reads and writes the memory the CLI
does. `plan-install` lists the approval as a change; an approved surface is not
listed again. Restart the host surface afterwards so it binds again.

What it does not do: it does not cover a session that carries a login. That one
is a named user like any gateway user and gets only what an audience row gives
it. It accepts no other platform: `cron` in particular stays refused, because
nobody is speaking in a scheduled run, a job can be created from any chat, and its
prompt would be captured as the owner's own words. Codex rejects the flag.

Approve a surface only where everyone who can reach it without logging in is the
owner. That is the same trust the CLI already has: whoever can run it against
this home can read the store.

A gateway route (Telegram, WeChat, Feishu, a Desktop login) is granted by an
audience row in `installation.json`, matched field by field against what the host
sends: `platform`, `user_id`, `chat_type`, `chat_id`, `thread_id`,
`gateway_session_key` and `agent_workspace`. A row whose `gateway_session_key` is
empty does not pin the host's session key, which is built from the platform, chat
type and chat the row already matches; a row that names one matches only that key.
For a plain, unthreaded chat a gateway sends an empty `thread_id`, so the row needs
`"thread_id": ""`; `main` is what the CLI and an approved local surface default to,
and an empty thread and `main` stay two routes. A session whose only near match
differs there binds with no scope and names it in its gaps:
`capability_gap:audience_thread_mismatch:row_says_main`.

It writes two wrapper files into the plugin directory (`__init__.py`,
`plugin.yaml`) and two skills under `<instance-root>\skills\`:
`scope-recall-setup\SKILL.md`, for installing and upgrading, and
`scope-recall-memory\SKILL.md`, which tells the agent how to answer what is
remembered about the user, where a memory came from and whether it still holds,
and what to say before it corrects, mutes or deletes one. A skill of the same
name that the installer did not write is never overwritten; `plan-install`
reports it as a conflict.

**Then enable it in the host.** Hermes registration means two things at once: the
entry point is importable in that interpreter, **and** that instance's
`<instance-root>\config.yaml` selects the provider:

```yaml
memory:
  provider: scope-recall
```

Do not edit a sibling or production home to do it. Until that key is set,
`doctor` reports `host_registration_status: "host_config_missing"` or
`"not_selected"` and the gap `host_registration_incomplete`.

The Core data directory is `<instance-root>\scope-recall\`, holding
`memory.sqlite3` and, once configured, a `vectors\` companion directory. Hermes
tools exposed by the adapter are `recall`, `inspect`, `profile`, `entity`,
`trace`, `revise`, `forget` and `status`, and it subscribes to the host hooks
`pre_llm_call`, `post_tool_call` and `api_request_error`.

## 5. Install the Codex MCP path

Same arguments, with `--host codex`. Here `--instance-root` holds
`codex-installation.json` and `data\`; `--target-plugin-dir` is the Codex plugin
directory; `--project-root` is the workspace root, which the installer writes
into `.mcp.json` as the MCP server's `--workspace`.

For Codex there is no host-sent identity to match: `--agent-id` is an identifier
you choose and the installation record keeps. It must stay the same across
re-installs of that instance, or `plan-install` reports an `agent_id mismatch`.
`--agent-workspace` is refused here; it is a Hermes concept.

```powershell
$Instance = "C:\path\to\codex-home\scope-recall"
$Plugin   = "C:\path\to\codex-home\plugins\scope-recall"
$Project  = "C:\path\to\your\repo"
$Python   = "C:\path\to\python.exe"

scope-recall plan-install --host codex `
  --target-plugin-dir $Plugin --instance-root $Instance `
  --project-root $Project --agent-id main --python $Python `
  --env-file "$Instance\embedding.env"

scope-recall apply-install --host codex `
  --target-plugin-dir $Plugin --instance-root $Instance `
  --project-root $Project --agent-id main --python $Python `
  --env-file "$Instance\embedding.env"
```

`--env-file` is Codex-only and worth understanding. It must be an absolute path to
a file that already exists; the installer checks that before planning. Codex starts
the MCP server and the hook processes with its own environment, which does not
contain the credential variable names your `runtime-config.json` declares. Given this file,
the installer writes its path into `.mcp.json`, `hooks.json` and the hook
launcher, and each entry process reads **only** the names the trusted config
declares — it is not a dotenv loader. Without it, `recall` inside Codex degrades
to purely lexical. Usually the same file is passed to `autostart enable`.

`apply-install` writes, into the plugin directory:

- `.codex-plugin\plugin.json`
- `hooks\hooks.json` and `hooks\scope-recall-hook.cmd` (the Windows launcher)
- `.mcp.json`, defining the MCP server named `scope-recall`
- `skills\scope-recall-setup\SKILL.md`

The installer owns these files exclusively. Do not hand-edit them or add your own
scripts to that directory: the next `plan-install` will report them as
`edited prior file` or `unrelated plugin file` and refuse. Change the installer if
you need different behaviour.

Six native hook events are registered, each invoking
`scope_recall.adapters.codex.hook_entry` through the isolated interpreter with a
2-second timeout: `Interrupt`, `PostToolUse`, `SessionEnd`, `SessionStart`,
`Stop`, `UserPromptSubmit`.

**Then enable it in Codex:**

1. Trust the written `hooks\hooks.json` in Codex. The installer generates files;
   it cannot approve them for you.
2. For the MCP tools, confirm the wheel was installed with the `[codex]` extra,
   and allow the server `scope-recall` through Codex's own MCP configuration. Its
   tools are `recall`, `inspect`, `profile`, `trace`, `entity`,
   `propose_memory`, `revise`, `forget` and `status`.

`hook_trust_status` stays `pending` in a read-only diagnosis; this project ships
no GUI and no separate trust command. Whether hooks really run is visible only in
Codex's own behaviour.

TODO(verify): the concrete Codex-side steps for trusting a plugin's native hooks
and allowing an MCP server are defined by Codex, not by this repository, and are
not derivable from this source tree. Follow Codex's own documentation for the
version you run; this installer only writes the files those steps consume.

The Core data directory is `<instance-root>\data\`, holding `memory.sqlite3`.

## 6. Verify with `doctor`

```powershell
scope-recall doctor --host hermes --instance-root C:\path\to\hermes-home --python C:\path\to\python.exe
```

```bash
scope-recall doctor --host codex --instance-root /path/to/codex-home/scope-recall --python /path/to/python
```

`doctor` accepts only `--host`, `--instance-root` and `--python`. It does not take
the install-time `--target-plugin-dir`, `--project-root` or `--agent-id`. With
`--python` it probes that interpreter and reports the package version, location
and any mismatch it finds there; without it, it measures itself, which cannot tell
you whether the host's environment has the new wheel.

`doctor` writes nothing. It prints one JSON object with about fifty fields, sorted
by key, and exits `0` only when `status` is `"ok"`.

### Reading the result

`status` has exactly three values:

| `status` | Exit | Meaning |
|----------|------|---------|
| `ok` | 0 | No gaps, no failed work, no blocked capture. |
| `attention` | 1 | No gap from the actionable set, but at least one from the non-actionable set below, or failed work, pending capture, or partial extractions. Worth a look, not an emergency. |
| `degraded` | 1 | At least one gap an operator must act on. It is also the fail-safe default when the database cannot be read at all. |

The four gaps that yield `attention` rather than `degraded` are
`vector_threshold_unconfigured`, `work_failed_terminal_only`, `work_needs_review`
and `worker_capability_unavailable`. Everything else forces `degraded`.

### A healthy report

Abridged — the real output has about fifty fields and more `checks` rows. These
are the ones to read first, from a healthy Hermes install:

```json
{
  "status": "ok",
  "capability_gaps": [],
  "host": "hermes",
  "host_registration_status": "registered",
  "hook_trust_status": "unknown",
  "binding_ok": true,
  "database_present": true,
  "package_ok": true,
  "package_version": "3.1.0rc39",
  "expected_package_version": "3.1.0rc39",
  "pending_work": 0,
  "failed_work": 0,
  "needs_review_work": 0,
  "capture_inbox": 0,
  "capture_inbox_blocked": 0,
  "checks": [
    {"name": "host_registration", "result": "registered"},
    {"name": "adapter_binding", "result": "ok"},
    {"name": "database", "result": "ok"},
    {"name": "schema", "result": "ok"},
    {"name": "work_backlog", "result": "idle"},
    {"name": "candidate_processing", "result": "idle"}
  ]
}
```

Things that look wrong in a healthy report and are not:

- `hook_trust_status: "unknown"` is the only value Hermes ever reports, and
  `"pending"` is the only value Codex ever reports. Neither produces a gap.
- On Codex, `host_registration_status: "pending"` is the healthy value —
  registration is not verified for that host, and `pending` is explicitly
  exempt from the gap.
- `running_code` with `result: "no_records"` simply means no process has bound
  this instance yet. A host registers when it binds an identity for a session.
- `worker_status: {}` means the worker has never written a receipt.
- `autostart_status: "not_registered"` and `ledger_headroom: {}` mean you have
  not configured those things, which is not a fault.
- `terminal_failed_work: null` on a clean queue.

### Common gaps and what they mean

| Gap | Cause | Fix |
|-----|-------|-----|
| `host_registration_incomplete` | For Hermes: the entry point is missing from the probed interpreter, or `config.yaml` is absent, or `memory.provider` is not `scope-recall`. Read `host_registration_status` for which. | Install the wheel into the host's environment, or set the provider key. A fresh install always shows this until you do. |
| `installation_config_missing` | The installer's own record is not there. | The install did not complete. Re-run `plan-install` and `apply-install`. |
| `binding_invalid:<Error>` | The installation record exists but will not load. | Do not hand-edit it; re-install. |
| `database_missing` | No `memory.sqlite3` in the Core data directory. | Nothing has initialised the instance. `apply-install` does that. |
| `runtime_config_missing` | `runtime-config.json` is gone from the Core data directory, but the store holds embeddings or consolidations that only a runtime config's routes could have run. Without it every host runs in basic mode and no worker runs. | Restore the file from its backup, or write it again (see [configuration.md](configuration.md)). A fresh install without one is not reported. |
| `storage_read:<Error>` | The database could not be read. `status` stays `degraded`. | Check permissions and whether another process holds it. To inspect the data without contending with a live writer, take a verified snapshot first with `scope-recall backup --database <db> --output <new-file>`, which refuses to overwrite anything and writes a manifest beside it. |
| `python_executable_missing` | The `--python` path is not a file. | Point it at the host's real interpreter. |
| `python_package_missing` | That interpreter could not report the package. | The wheel is not installed in that environment. |
| `python_package_version_mismatch` / `python_package_metadata_mismatch` | The loaded version differs from this tree's, or from the installed distribution metadata. | Reinstall the wheel; do not patch files in place. "It imports" is not "it is installed". |
| `hot_patched` | Installed files no longer match the wheel's recorded digests. | Reinstall. Editing installed files is the usual cause. |
| `dependency_drift` | A declared requirement is missing or outside its pin. Extras you did not install are *not* drift. | Reinstall with the pins, or install the extra properly. |
| `version_mismatch` | Receipt, distribution, imported and running versions disagree. | Stop the old processes, then reinstall. |
| `stale_process` | A live process is running code older than what is on disk. | Restart the host, or let the running worker finish. |
| `schema_upgrade_pending` | The store is at an older schema this code knows how to bring forward. | Nothing to run: the next capture, recall or worker pass applies it in one transaction, rolled back whole if it fails. Take a `backup` first if you want one. |
| `schema_version_mismatch` | The database schema is one this code cannot bring forward. | Do not run against it. Back it up and use the migration path. |
| `schema_header_stale` | The store's tables and its own record say one schema, the SQLite header another: another process stamped the header, typically a 2.0 plugin that opened the store after its migration. Every open is refused. | Stop that process, then run `upgrade-store` with `--backup-dir`: it snapshots the store and puts the recorded schema back into the header. |
| `vector_threshold_unconfigured` | A vector store and an approved embedding route are configured, but no threshold is set, so every vector hit is refused and recall stays lexical. `attention`. | Set a `vector_threshold` calibrated for that embedding model — see [configuration.md](configuration.md). |
| `work_failed` | At least one recoverable failure is queued. | Fix the cause, then `scope-recall retry-failures --config <file> --apply`. |
| `work_failed_terminal_only` / `work_needs_review` | All failures are by design, or were already retried once. `attention`. | Inspect them; `--include-terminal` re-runs them only if you mean to. |
| `work_backlog_stalled` | Work is pending and the worker has not succeeded for more than twice `supervisor_seconds`. | The worker is not running. See the next section. |
| `worker_capability_unavailable` | Work is pending and the last pass reported work types it could not do. `attention`. | Usually a missing model route, credential or budget. |
| `capture_ingress_blocked` | Inbox rows carry a real error code. Always `degraded`. | Read `capture_inbox_blocked` and the recent work errors. |
| `autostart_registration_missing` | The control file says enabled, but the scheduled task is gone. | Re-run `autostart enable`. |
| `autostart_configuration_invalid` | `runtime-autostart.json` is unusable, or points at a config that will not load or does not match the binding. | Re-run `autostart enable` with the correct `--config`. |
| `ledger_missing:<file>` | An external route is approved but its budget ledger file does not exist. | Create the ledger — see [configuration.md](configuration.md). |
| `model_not_approved:<role>:<model>` | The route's model is not in `budget.approved_models`. | Add it, with pricing. |
| `model_refused:<model>:<code>` | Over the last hour, most calls to that model were refused by the provider. | A credential, quota or spend-cap problem at the provider. |
| `auxiliary_budget_pressure` | A lifetime call or token cap is at 90 % or more. | Raise the cap deliberately, or accept the stop. |

A note on the shape: `checks[]` entries use the key `result`, not `status`, and
their vocabulary is per-check. The value `attention` appears only in the report's
own top-level `status`.

## 7. Enable background work

Consolidation and embedding happen in a bounded worker, not a resident service.
Hosts wake it as they capture; a scheduled wake covers the idle case.

### Windows: the scheduled task

Autostart is **Windows Task Scheduler only**. `maintenance/autostart.py` drives
`schtasks.exe`, and `apply` refuses anything else with `autostart_windows_only`.
There is no cron, systemd or launchd integration anywhere in this distribution.

```powershell
scope-recall autostart plan --config C:\path\to\instance-root\scope-recall\runtime-config.json --python C:\path\to\python.exe
scope-recall autostart enable --config C:\path\to\instance-root\scope-recall\runtime-config.json --python C:\path\to\python.exe --env-file C:\path\to\instance-root\scope-recall\embedding.env
scope-recall autostart pause  --config C:\path\to\instance-root\scope-recall\runtime-config.json
scope-recall autostart remove --config C:\path\to\instance-root\scope-recall\runtime-config.json
```

- `plan` builds and prints the task XML and validates everything without
  registering: absolute `--config` and `--python`, the config sitting directly
  inside the binding's data directory, a readable database, and an absolute
  existing `--env-file` if given. It changes nothing.
- `enable` registers the task. `pause` disables it; `remove` deletes it. Both read
  the control file the registration wrote.
- `--python` defaults to the interpreter running the command, which is usually not
  what you want — pass the host's interpreter explicitly. `--user-id` defaults to
  the current account.
- Failures print one JSON object with a `code` and exit 2.

The registered task triggers at that user's logon and then every 5 minutes, runs
hidden at least privilege with a 1-minute execution limit, and invokes
`scope_recall.runtime.resume_entry`, which decides whether a wake is actually due
and launches a detached worker if so. `supervisor_enabled: false` in
`runtime-config.json` makes every wake a no-op without unregistering the task.

Two caveats on non-Windows: `autostart plan` still succeeds there, because it only
builds XML — a successful `plan` is not a registration. And `pause` / `remove`
call `schtasks.exe` unconditionally, so on Linux or macOS they raise rather than
printing the usual error object.

### Everywhere: run a pass by hand

One bounded pass, in the foreground:

```powershell
C:\path\to\python.exe -m scope_recall.runtime.worker_entry --config C:\path\to\instance-root\scope-recall\runtime-config.json
```

```bash
/path/to/python -m scope_recall.runtime.worker_entry --config /path/to/instance-root/scope-recall/runtime-config.json
```

`--config` must be absolute. It prints one compact JSON line — `status`,
`processed`, `completed`, `failed`, `capability_gaps`, the queue counts — and
exits `0` when the pass ran (including a `degraded` or `idle` pass), `75` when
another worker or the truth writer held the lock, and `1` on an unexpected error.
It is a single pass with no supervisor loop: run it again, or from your own
scheduler, wherever autostart is unavailable.

This command has no `--env-file`. If an external route is configured, export the
credential variable named by `credential_env` into your shell before running it.

To re-open failures after shipping a fix:

```bash
scope-recall retry-failures --config /path/to/instance-root/scope-recall/runtime-config.json --apply
```

Without `--apply` nothing is written. `--include-terminal` also re-runs failures
that are terminal by design.

Since 3.2.0rc6 a tool output is kept and embedded, found by its words and by
meaning, but no longer consolidated into claims: what an agent read or ran is
not what it should remember, and how a task was done belongs to the host's
skills. To retire the unconfirmed claims an earlier release derived from tool
output alone:

```bash
scope-recall retire-rootless-claims --config /path/to/instance-root/scope-recall/runtime-config.json --limit 32 --apply
```

It works one page at a time: carry the printed `last_ref` into `--after-ref`
until it comes back empty. Without `--apply` it only lists what it would retire,
by reference, never by text. A retired claim gets a retracted version and stops
waiting for evaluation; its sources, its earlier versions and every confirmed
claim stay as they are.

## 8. Uninstall (memory is retained by default)

Uninstall is driven by the install receipt. **By default it removes only the
plugin wrapper files and keeps the Core database.** It also removes the Windows
wake task bound to that instance, if one is registered. Inspect the plan first:

```powershell
scope-recall plan-uninstall --instance-root C:\path\to\instance-root
scope-recall apply-uninstall --instance-root C:\path\to\instance-root
```

- `plan-uninstall` exits 1 when `conflicts` is non-empty.
- `--target-plugin-dir` may be omitted; it is read from the receipt.
- A plain `plan-uninstall` does not evaluate a purge at all: `purge_allowed` is
  always `false` in its output.
- `apply-uninstall` reports `memory_retained: true` while `memory.sqlite3` is
  still there. Files it cannot verify against the receipt are listed as
  `edited_files` and left alone.

Deleting the Core data is a **separate**, explicitly flagged operation, and
`--purge` must be on both steps:

```powershell
scope-recall plan-uninstall --instance-root C:\path\to\instance-root --purge
# only if that printed purge_allowed: true with no conflicts
scope-recall apply-uninstall --instance-root C:\path\to\instance-root --purge
```

A purge verifies the installation identity, that the data directory is really
owned by this installation, that no writer is active, and that no restore is
outstanding. If any check fails it refuses with a `purge_refused:*` reason and
deletes nothing. **Do not treat purge as a normal uninstall step.** Ordinary
uninstall does not need it.

## 9. Coming from an older database

Migrating from the legacy Hermes Scope Recall SQLite baseline is an offline,
explicit operation, separate from a normal install:

1. Stop the old plugin from writing and back up its `memory.sqlite3` (and any
   `vectors\` directory).
2. Do the empty-instance install above, in a new instance directory.
3. Follow [upgrade-guide.zh-CN.md](upgrade-guide.zh-CN.md) to run the migration job.
4. Check the migration report and sample the migrated memories **before**
   pointing the host at the new plugin. Keep the old database and the old
   install; clean up by hand only once you are satisfied.

There is no one-click upgrade from 2.x and no long-term v3 compatibility
layer for it.

Upgrading between 3.x versions is different. Install the new wheel; the first
ordinary open of the store afterwards (a capture, a recall, a worker pass)
applies the schema upgrade in one transaction and rolls it back whole if it
fails. `doctor` reports `schema_upgrade_pending` until then and never applies
the upgrade itself. Take a `backup` first if you want one. On a store above
100 MB a step that rebuilds an index (3.1.1's lexical index: about a minute
and a half for five million index rows) is left to a caller with the time
for it, the worker's next pass or `apply-install`; a hook's open reports
`SCHEMA_UNSUPPORTED / upgrade_pending` until then. To do it now, with a
snapshot first and the worker stopped:

```bash
python -I -X utf8 -m scope_recall.maintenance.cli upgrade-store --host hermes --instance-root <instance root> --backup-dir <a new directory>
```

It reports the schema before and after, the seconds taken and the journal
mode, and leaves a store a running worker holds untouched (`store_busy`).

If a 2.0 plugin opens a migrated store, it stamps the SQLite header with the
2.0 layout's schema (10815) while every 3.x table and the store's own record
(`instance_meta.schema_version`) stay as they were, and every open is refused
with `SCHEMA_UNSUPPORTED / header_stale:run_upgrade_store`; `doctor` reports
`schema_header_stale`. Stop every 2.0 process first, then run the same
`upgrade-store` command: after its snapshot it writes the recorded schema
back into the header (`header_restamped` in its report) and, if that schema
is an older one, brings the store forward as usual. A store that is not this
product's, or records no schema this release knows, is still refused.

## 10. Platform and storage boundaries

- **The store is a WAL-mode SQLite file.** `memory.sqlite3-wal` and
  `memory.sqlite3-shm` sit beside `memory.sqlite3` while any process has it
  open. Never copy the files by hand while the host or the worker runs;
  `scope-recall backup` takes a consistent snapshot and writes it in
  rollback-journal mode. Readers and the writer coexist, so an operator
  query no longer fails a worker pass.

- **LanceDB** needs the `lancedb` extra. Keep the data directory short, for
  example `C:\ScopeRecall\my-agent`: LanceDB appends index, table and temporary
  file names below it, and the worker reports `native_vector_path_too_long`
  before touching LanceDB or the embedding API when the result is too long.
- **PostgreSQL / pgvector** is not in this distribution. Configuring it is an
  explicit error; keep the old installation and use the migration guide.
- **`runtime-config.json` is never generated.** Without it the Core runs with
  basic capability and the host reports a capability gap. Everything it can set —
  the budgets, the vector store, the model routes — is documented in
  [configuration.md](configuration.md).

## 11. One store for several agents

Everything above installs one agent with its own store. Several agents can instead
share one store, each attached to it as an entry, with every memory marked with the
agent it came in through: [shared-store.md](shared-store.md). An attached home keeps
only a pointer, `scope-recall\attachment.json`; `plan-install`, `apply-install` and
`doctor` recognize it.

## Names and paths

| Concept | Value |
|---------|-------|
| Distribution name | `hermes-scope-recall` |
| Python import | `scope_recall` |
| Host plugin identity | `scope-recall` |
| Console commands | `scope-recall`, `hermes-scope-recall` |
| Install receipt | `<instance-root>\.scope-recall-install-receipt.json` |
| Overwrite backups | `<instance-root>\.scope-recall-backups\` |
| Hermes installation record | `<instance-root>\scope-recall\installation.json` |
| Hermes Core data directory | `<instance-root>\scope-recall\` |
| Codex installation record | `<instance-root>\codex-installation.json` |
| Codex Core data directory | `<instance-root>\data\` |
| Runtime config | `<core-data-directory>\runtime-config.json` |
