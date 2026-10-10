# Installing Scope Recall

Scope Recall is a bounded local memory core for coding agents: SQLite holds the
truth, vector indexes are rebuildable companions, and a bounded background worker
does the consolidating and embedding. It ships host adapters for Hermes and for
Codex, the latter as a set of native hooks plus an MCP server, which Claude Code
uses too. This guide installs Hermes and Codex with a store of their own; Claude
Code installs only as an entry of a shared store, and Codex can join one too
(section 11). WorkBuddy runs the same hooks and installs only as an entry as well
(section 12), and so does DeepSeek Harness (dsh), through a plugin that runs those hooks
(section 13).

> **Status.** This guide covers 3.1 to 3.7. Releases are on PyPI and on the
> GitHub releases page; a checkout between releases carries a candidate version
> and is installed by building its wheel. The distribution name is
> `hermes-scope-recall`, the Python import is `scope_recall`, and the host plugin
> identity is `scope-recall`.

v3 has no automatic `update` / `upgrade` / `rollback` commands. Moving data from an
older database is a separate, explicit operation — see
[upgrade-guide.zh-CN.md](upgrade-guide.zh-CN.md).

## 1. Requirements

- **Python 3.11 to 3.14.** `pyproject.toml` declares `requires-python = ">=3.11,<3.15"`;
  3.15 and newer are not supported yet.
- Install into **the same isolated Python environment the host uses**. Host
  discovery goes through that environment's package metadata.
- **A Hermes that builds its own environment** (Hermes Desktop builds the environment it runs
  plugins in, and builds it again on updates, which drops a core installed there by hand): the
  plugin `apply-install` writes declares the core it runs on,
  `pip_dependencies: hermes-scope-recall[lancedb]==<version>`, and Hermes installs what a memory
  provider declares when the provider is set up (in its dashboard, or `hermes memory setup`).
  Hermes reads that declaration from `<home>\plugins\scope-recall\` or from the installed
  core's own directory, and once the core is gone only the first is left: give such a Hermes
  that directory as `--target-plugin-dir` (section 4). A plugin whose core is missing says so
  when Hermes loads it; setting the provider up again installs the declared release.
  A final release is declared, which Hermes resolves from PyPI: a wheel installed before its
  release reaches PyPI declares a requirement Hermes cannot resolve yet, so run `apply-install`
  on such a home once it has. A candidate between releases declares nothing, because a
  requirement Hermes cannot resolve fails its whole build: install a candidate by hand into the
  environment Hermes runs, and again after Hermes rebuilds it.
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
point of the installed package**, or the wrapper `apply-install` writes into
`<home>\plugins\scope-recall` when you give it that directory (section 4). Do not
copy or symlink a directory into the host's plugin folder by hand.

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

WorkBuddy is the one exception: it has no plugin directory an installer could own,
so `apply-install --host workbuddy` adds its entries to WorkBuddy's own settings
files, and keeps everything else in them (section 12).

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

The three roots may not overlap in either direction, with one exception:
`--target-plugin-dir` may be `<instance-root>\plugins\scope-recall`, the directory
Hermes itself looks a memory provider up in. Use it for a Hermes that builds its
own environment (section 1): Hermes then finds the plugin, and the core it
declares, even after a rebuild dropped the core. Anywhere else the wrapper sits
outside the home, and discovery comes from the installed package's entry point.
The directory name itself must match `^[a-z][a-z0-9-]*$`.

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
is approved apart (`--owner-login`, below) or gets only what an audience row gives
it. It accepts no other platform: `cron` in particular stays refused, because
nobody is speaking in a scheduled run, a job can be created from any chat, and its
prompt would be captured as the owner's own words. Codex rejects the flag.

Approve a surface only where everyone who can reach it without logging in is the
owner. That is the same trust the CLI already has: whoever can run it against
this home can read the store. A host that serves its dashboard to other machines
(`hermes serve --host 0.0.0.0` with `dashboard.basic_auth`) runs the dashboard's
Chat tab as `hermes --tui` on the host for whoever logged in, and passes that
login to no memory provider, so such a session names no user. Approving `tui` (or
`desktop`) on that host gives every dashboard login the owner's memory through
the Chat tab.

#### A dashboard login: `--owner-login`

With a dashboard login (`hermes serve` with `dashboard.basic_auth`, or the Desktop
app connected to such a host from another machine) the host passes the login to
the adapter as the session's user, `basic:<name>`, and names no chat. The adapter
routes that session as a one-to-one chat with the login: `chat_type` `private`,
`chat_id` the login, `thread_id` `main`. A login is not the local owner. Until it
is approved it binds no scope, nothing said there is captured or recalled, and the
host log says so once per session:

```
scope-recall: session bound to no memory scope: a desktop session for basic:alice (capability_gap:audience_unmapped, capability_gap:owner_private_denied, capability_gap:no_allowed_scope); nothing in it is captured or recalled; if this login is the owner's own, approve it with apply-install --owner-login desktop=basic:alice
```

Approve your own login with `--owner-login <platform>=<login>`, repeatable,
`desktop` or `tui` only, with the login exactly as the host sends it:

```powershell
scope-recall apply-install --host hermes `
  --target-plugin-dir $Plugin --instance-root $Instance `
  --project-root $Project --agent-id default --python $Python `
  --owner-login desktop=basic:alice
```

It adds the owner principal `(desktop, basic:alice)` and one grant of the owner's
private scope on that route, the way `--local-platform` does for a session that
names no user, with the same backup, and approves nothing else: not that login on
`tui`, not another login, not a session that names no user. Whoever holds that
login then reads and writes the owner's private memory there, from any machine
that reaches the host, so approve only a login that is the owner's own; `revise`
and `forget` stay with the CLI. A shared store entry keeps the grants it was
attached with: approve a login in the home's own installation before attaching
it. `doctor` reports an owner row whose user is no owner principal as
`audience_owner_unverified`.

A login that is not the owner's is a user like any gateway user and gets only
what an audience row on its route gives it.

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
`pre_llm_call`, `post_tool_call`, `post_llm_call` and `api_request_error`.
`post_llm_call` is read at the end of a turn for what the assistant showed
between its tool calls; Hermes hands the memory provider only the answer.

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
you need different behaviour. A skill (`SKILL.md`) an agent edited is the one
exception: while the package's copy of it is the one installed before, the install
keeps the edit (`kept` in the plan) and installs the rest; once a release changes
that skill, the edit is a conflict again.

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

Each store under `index_metadata.vector_stores` names its nearest-neighbour index
in `index_outcome`. The worker builds the index once the store holds 10,000
vectors, at the start of a pass that has the time for the build: about 8 s for
78,000 vectors of 3,072 dimensions. It is IVF over 8-bit quantized vectors, and a
search probes every partition and re-ranks its nearest candidates exactly, so it
finds what an exact scan finds, in about 40 ms instead of 750. A vector index of
another kind is replaced (`rebuilt`). The same outcome is in
`vectors/<space>/index-state.json`.

| `index_outcome` | Meaning |
|-----------------|---------|
| `built`, `rebuilt` or `present` | The store has its index; compaction keeps it current. |
| `below_threshold` | Fewer than 10,000 vectors; an exact scan is quick enough. |
| `deferred` | The build did not fit the pass; a later pass with more time builds it. |
| `failed` | The build failed; it is tried again six hours later. |
| `started` | A build began and no outcome was recorded (the pass was ended); tried again six hours later. |
| none | No pass has looked yet. |

Without the index a search reads every vector. A Claude Code or Codex hook starts
its search helper fresh for each prompt, so on a large store its automatic recall
answers from words alone.

Each store also says how far the worker got in giving an import's history the
embeddings its old store never had (`embed_backfill_outcome`, with
`last_embed_backfill_at` and `embed_backfill_queued_total`, the embeddings queued
so far). Every pass tops the embedding queue up with the next of the owner's
messages, replies, documents and notes that an import brought in without one:
to 64 embeddings waiting, or to half a pass (16 at the default 32 items) while a
candidate evaluation the pass would take is ready, since the worker takes
embeddings first. Once none is left it looks again a day later. Each worker keeps
its place in `vectors/<space>/embed-backfill-<partition>.json`. Where several
workers share a store (a local install keeps one for each project and branch),
the outcome is a failed one's, else the latest; a worker's file that nobody has
looked at for two days is left out of it.

| `embed_backfill_outcome` | Meaning |
|--------------------------|---------|
| `progress` | A page was queued; the next pass goes on from there. |
| `held` | The queue was full: 64 embeddings waiting (captured messages' or the previous page's), or half a pass while a candidate evaluation the pass would take was ready; nothing was queued. |
| `finished` | No import is left without one; looked at again after a day. |
| `failed` | The page could not be read (`embed_backfill_error` names the error); that worker tries again at every pass. Those memories are found by their words until it passes. |
| none | No pass has looked yet, or the store has no embedding route. |

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
  "capture_inbox_given_up": 0,
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
  not configured those things, which is not a fault. `"operator_timer"` is an
  enabled wake outside Windows, run by a timer you installed (section 7).
- `unreached: []` means no partition's work has waited more than a day, apart from
  work no configured route can do or a provider holds.
- `terminal_failed_work: null` on a clean queue.
- `pending_error_counts` is the error queued work kept from its last attempt. That
  work is waiting for another attempt, so it is in none of the failure counts.
- `embedding_respace: null` means no re-embed run was ever started (see
  section 7). `embedding_health` always counts the embedding queue; its
  `last_day` and `held_until` appear only with an external embedding route,
  and only while the provider holds it.

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
| `schema_upgrade_pending` | The store is at an older schema this code knows how to bring forward. | Nothing to run: the next capture, recall or worker pass applies it in one transaction, rolled back whole if it fails; on a store above 100 MB, a caller with a minute of budget does (section 9). A step is one way: take a `backup` first if you may want to go back. |
| `schema_version_mismatch` | The database schema is one this code cannot bring forward. | Do not run against it. Back it up and use the migration path. |
| `schema_header_stale` | The store's tables and its own record say one schema, the SQLite header another: another process stamped the header, typically a 2.0 plugin that opened the store after its migration. Every open is refused. | Stop that process, then run `upgrade-store` with `--backup-dir`: it snapshots the store and puts the recorded schema back into the header. |
| `vector_threshold_unconfigured` | A vector store and an approved embedding route are configured, but no threshold is set, so every vector hit is refused and recall stays lexical. `attention`. | Set a `vector_threshold` calibrated for that embedding model — see [configuration.md](configuration.md). |
| `audience_owner_unverified` | An `owner_private` audience row names a user who is no owner principal, so it grants nothing and every session on its route captures and recalls nothing. The `audiences` check counts such rows by platform. `attention`. | If the user is the owner's own dashboard login, approve it with `apply-install --owner-login <platform>=<login>` (section 4); otherwise remove the rows. |
| `work_failed` | At least one recoverable failure is queued. | Fix the cause, then `scope-recall retry-failures --config <file> --apply`. |
| `work_failed_terminal_only` / `work_needs_review` | All failures are by design, or were already retried once. `attention`. | Inspect them; `--include-terminal` re-runs them only if you mean to. |
| `work_backlog_stalled` | Work is pending and the worker has not succeeded for more than twice `supervisor_seconds`. | The worker is not running. See the next section. |
| `worker_capability_unavailable` | Work is pending and the last pass reported work types it could not do. `attention`. | Usually a missing model route, credential or budget. |
| `capture_ingress_blocked` | Inbox rows carry a real error code, or wait for their next try. Always `degraded`. | Read `capture_inbox_blocked` and the recent work errors. A row whose stored capture a replay could not check again is tried after a minute, doubling to an hour; when its 24th try again fails it is given up and counted in `capture_inbox_given_up`. `retry-failures` without `--apply` counts them by what gave them up (`inbox_by_kind`); fix that, then `retry-failures --apply` returns them to the replay. |
| `embedding_backlog_aged` | An external embedding route is configured, and embeddings have waited more than 24 hours. Recall goes on answering, but finds what came in since then by its words alone. The check's detail names the provider's hold and its refusals over the last day when there are any (`embedding_health`). Without a route nothing embeds, by choice, and the gap is not raised. | With a hold or refusals: a quota, a spend cap or a credential at the provider; fix it there and the queue drains by itself. Without them no worker has reached the embeddings: read `worker_status`, and where each project has a worker of its own, check that it runs. |
| `due_work_unreached` | Work, or a candidate still marked with new first-hand evidence, has waited more than 24 hours in some partition of the store, including work whose worker's lease ran out. A partition is worked only by a worker of its own audience. Work no configured route can do, and work a provider holds, is left out; a queue longer than its passes reach in a day is named too. `unreached` names each partition (scope ids carry chat and account ids, so share it with care). `attention`. | Give that audience a wake: on Windows `autostart enable`, elsewhere a timer (section 7). On a shared store the shared worker's wake works every scope its config lists. A channel no one uses any more is drained when a session of it opens; a long queue drains by itself. |
| `embedding_respace_space_mismatch` | A re-embed run (`respace-embeddings`) embeds into one space while `runtime-config.json` embeds into another, after a second change of model, so no worker goes on with it. | `respace-embeddings --config <file> --restart --apply` to start again into the new space, or `--cancel --apply`. |
| `embedding_respace_failed:<Error>` | A worker pass could not reopen the run's next page; the worker status carries it. The run is unchanged and the next pass tries again. | Read the error; a held writer lease passes by itself. |
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
Hosts wake it as they capture; a scheduled wake covers the idle case. A worker
stays up while a candidate is inside its quiet window and drains when the window
closes (its supervisor waits with `reason: "candidate_settle_window"`), so the
pass that ends a conversation no longer leaves the last messages' candidates for
the next session. A pass that could not look at them (a provider hold, the
evaluation queue full, a pass kept out, cut short or failed) holds that wake for
15 minutes. A worker that has stood down, or never started, needs the wake.

### Windows: the scheduled task

On Windows `maintenance/autostart.py` registers a task through `schtasks.exe`.
Elsewhere it prints the same wake for a timer of your own (next section).

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

### Linux and macOS: a timer of your own

Nothing outside Windows registers a wake: without one, a partition's queue drains
only while a host session of it runs. Run the same wake from a timer:

```bash
scope-recall autostart plan   --config /path/to/instance-root/scope-recall/runtime-config.json --python /path/to/venv/bin/python
scope-recall autostart enable --config /path/to/instance-root/scope-recall/runtime-config.json --python /path/to/venv/bin/python --env-file /path/to/instance-root/scope-recall/embedding.env
```

- `plan` validates as on Windows and prints, as one JSON object, the wake as
  `wake_command`, as a systemd user service and timer (`systemd_service`,
  `systemd_timer`) and as a crontab line (`cron`). It changes nothing. No
  `--user-id` is needed: the timer runs as the user who installs it. A path that
  holds a line break, or a backslash before `%`, is refused
  (`autostart_path_unsupported`); `%`, `$` and other backslashes are escaped.
- `enable` writes only `runtime-autostart.json`, the control file the wake reads,
  and registers nothing. Install the timer yourself, taking the texts out of the
  JSON as they are:

  ```bash
  scope-recall autostart plan --config <config> --python <python> > plan.json
  name=$(jq -r .task_name plan.json)
  mkdir -p ~/.config/systemd/user
  jq -r .systemd_service plan.json > ~/.config/systemd/user/$name.service
  jq -r .systemd_timer plan.json > ~/.config/systemd/user/$name.timer
  systemctl --user daemon-reload && systemctl --user enable --now $name.timer
  ```

  (`loginctl enable-linger <user>` keeps user timers running while you are logged
  out.) Or add the line `jq -r .cron plan.json` prints with `crontab -e`.
- The wake works the audience of `runtime-config.json` (its scopes, project and
  branch): a channel whose scope it lists is drained by it, any other only by its
  own sessions. Its first run with no pass on record (after an upgrade, or when
  only session workers ran) launches a worker for the candidates ready by then.
- Every 5 minutes the wake does what the Windows task does: `resume_entry`
  launches a detached worker only when work is due and no worker runs, with the
  credentials from `--env-file`. `pause` disables it in the control file (the
  timer goes on firing and does nothing); `remove` marks it removed. Remove the
  timer yourself.
- The doctor reports `autostart_status: "operator_timer"`. It cannot see the
  timer, but work that has waited a day is named by `due_work_unreached`.

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

If an external route is configured, pass its credentials file with `--env-file`
(absolute), as the wake does, or export the variable named by `credential_env`
into your shell before running it.

To re-open failures after shipping a fix:

```bash
scope-recall retry-failures --config /path/to/instance-root/scope-recall/runtime-config.json --apply
```

Without `--apply` nothing is written. `--include-terminal` also re-runs failures
that are terminal by design. The same command returns to the replay the captures
the inbox gave up after their tries (`inbox_given_up` in its output, and what gave
them up in `inbox_by_kind`; without `--apply` it only counts them), each with its
tries counted anew (one given up while it was being given a new key goes back to that
step). It reaches the rows of the partition its config replays, while
doctor's `capture_inbox_given_up` counts the whole store: in a shared store, run it
with the shared worker's config (`<root>\runtime-config.json`), since an entry's own
config reaches only that entry's scopes. Run it after going back to an earlier
release and forward again: a capture the earlier release could not read may have
been given up meanwhile.

Since 3.7.5 it also brings back the vector work of claim heads. Before 3.7.5 the
automatic recovery made a claim's failed embedding obsolete instead of retrying
it, and an earlier conversion left some heads without one. The command reopens
the first (`claim_embeds_reopened`) and queues the second
(`claim_embeds_queued`), a page at a time up to `--limit`. It does this only for
heads that are readable in its config's scopes. A claim found by its words alone
is then found by meaning too.

After changing the embedding model (see [configuration.md](configuration.md),
"Changing the embedding model rebuilds the vector store"), re-embed what was
embedded so far into the new space:

```bash
scope-recall respace-embeddings --config /path/to/instance-root/scope-recall/runtime-config.json --start --apply
```

Without `--apply` nothing is written, and without `--start` the command shows
the run, the embeddings it still has to reopen (`to_reopen`) and those still
waiting in the store (`waiting`).

- Start right after switching. The run reopens every embedding finished before
  it starts, so whatever waited at the switch, or was embedded into the new
  space before the start, is paid for twice. While the old route still answers,
  let `waiting` come down before you switch.
- The run covers the whole store and lives in SQLite. Every worker pass in that
  space reopens a page of finished embeddings, newest first. It does so only
  while fewer than 64 embeddings wait anywhere in the store, or half a pass's
  worth while candidate evaluations are ready, so messages captured meanwhile
  are embedded first. Embeddings of a project whose worker never runs hold the
  run; the doctor's `embedding_respace` check then says how many wait.
- `--start` refuses while a run is going, and after one into the same space has
  finished. `--restart --apply` starts again from the newest. `--cancel --apply`
  stops the run, and what it reopened is still embedded.

Since 3.2.0 a tool output is kept and embedded, found by its words and by
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

An earlier release's capture filter left a one-line summary in place of a tool
output it withheld ("Tool execution summary ... output_preview=omitted"); stores
that imported that history hold many. Since 3.7.4 such a placeholder is found by
the tool's own error text alone, when it carries one, and otherwise by nothing:
the rest of its words are the envelope's own. Earlier releases, the 1109 upgrade
and both imports indexed the whole placeholder, and on a store that imported
many they push ordinary words such as "tool", "status" and "patch" past the
common-term ceiling, so questions lose those words. To drop the postings beyond
their error text:

```bash
scope-recall unindex-withheld-outputs --config /path/to/instance-root/scope-recall/runtime-config.json --until-done --apply
```

Without `--apply` it only counts the placeholders and their postings. Each page
(`--limit`, 500 by default, at most 5,000) is its own write transaction, which
holds the store's writer lease while it runs; `--until-done` pauses a moment
between pages so captures get it. A page cut short is simply found again, so it
can be stopped and run again at any time; without `--until-done`, carry the
printed `next_after_id` into `--after-id` while `more` is true. The sources stay,
and so does the index of their error text. In a shared store, run it with the
shared worker's config (`<root>\runtime-config.json`), which reaches every scope.
On the shared store's copy it dropped 1,945,720 postings of 212,773 placeholders
in 426 pages, the slowest 0.12 s, and kept the error text of the 4,348 that carry
one.

To re-frame the claims an earlier release stored in a frame this release would
not write, among them a name given for the first time ("my cat is called ...")
filed as an alias nothing could ever confirm:

```bash
scope-recall repair-claim-frames --config /path/to/instance-root/scope-recall/runtime-config.json --limit 16
```

It writes as it goes, one page at a time: carry the returned `cursor` into
`--after-ref` until `done` is true. It makes no model calls and never rewrites
source text or earlier versions; take a `backup` first.

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
100 MB any pending step is left to a caller with a minute of budget or none:
the worker's next pass, `apply-install`, `upgrade-store`, or a Hermes session
starting (its status read carries no deadline); a hook's bounded open reports
`SCHEMA_UNSUPPORTED / upgrade_pending` until then. For scale, 3.1.1's lexical
index took about a minute and a half for five million index rows, and 3.2.0's
step reads every source row, about 3 s a gigabyte.

A step is one way. Once a newer release has opened the store, an older one
refuses it (`SCHEMA_UNSUPPORTED`) without touching it: a 3.1 process cannot
open a store 3.2 has opened. Stop every host and worker of a store before
upgrading, upgrade them together, and keep the pre-upgrade `backup`; going
back means restoring it, and writes made since are lost.

To bring a store forward now, with a snapshot first and the worker stopped:

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
share one store, Hermes agents and from 3.3.0 Codex and Claude Code, each attached to
it as an entry, with every memory marked with the agent it came in through:
[shared-store.md](shared-store.md). Claude Code installs only this way. An attached home keeps
only a pointer, `scope-recall\attachment.json`; `plan-install`, `apply-install` and
`doctor` recognize it.

## 12. WorkBuddy

WorkBuddy runs the hooks and the MCP server of the `codex` adapter as an entry of a shared
store, the owner at this machine. Attach its home first ([shared-store.md](shared-store.md)),
from an environment with the `codex` extra, then install. WorkBuddy's agent reads its hooks from
`settings.json` in WorkBuddy's own home. Its MCP servers are those listed in `mcp.json` there:
WorkBuddy starts each one, once you have approved it in its MCP settings, and serves its tools to
the agent. (`.mcp.json` beside it is WorkBuddy's record of its own connector proxy; its agent
reads no other server from it.) WorkBuddy may write `settings.json` itself while it runs: quit
WorkBuddy before `apply-install` and start it again after, then approve the server `scope-recall`,
which WorkBuddy lists as waiting for approval. WorkBuddy reads `mcp.json` when it starts, so the
server shows only after that restart. In WorkBuddy 5.6.2 the approval is under 专家·技能·连接器
(Experts · Skills · Connectors) → 连接器 (Connectors) → 自定义连接器 (Custom connector, the ⊕ at
the top right) → MCP 服务管理 (MCP Server Management) → 我的 MCP (My MCP) → 信任 (Trust).

```powershell
$Entry  = "D:\ScopeRecall\workbuddy"
$Python = "D:\ScopeRecall\workbuddy-venv\Scripts\python.exe"

scope-recall plan-install --host workbuddy --instance-root $Entry `
  --agent-id <the store's agent id> --python $Python --env-file <the file with the embedding key>
# quit WorkBuddy
scope-recall apply-install --host workbuddy --instance-root $Entry `
  --agent-id <the store's agent id> --python $Python --env-file <the file with the embedding key>
# start WorkBuddy
```

`--target-plugin-dir` names WorkBuddy's home; without it the installer uses
`WORKBUDDY_CONFIG_DIR`, else `%USERPROFILE%\.workbuddy`, and refuses a home that does not
exist. `apply-install`:

- adds one command hook each for `UserPromptSubmit`, `Stop` and `SessionEnd` under `hooks` in
  `settings.json`, and the MCP server `scope-recall` under `mcpServers` in `mcp.json`;
- keeps every other key, hook and server as it is, and copies each file it changes to
  `<instance-root>\.scope-recall-backups\<id>\plugin\` first (`backups` and `files_merged` in
  its output); neither file enters the receipt;
- changes nothing when run again; this entry's hook from an older interpreter or env file is
  updated where it stands;
- refuses, and writes nothing, when the settings already run another Scope Recall hook (another
  entry's, or a remote client's: WorkBuddy would run both), when `mcp.json` has a `scope-recall`
  server that is not this entry's, or when a file is not plain JSON (WorkBuddy accepts comments;
  rewritten as JSON they would be lost, so add the entries by hand there).

On Windows WorkBuddy runs a hook through Git Bash, so Git for Windows must be installed; without
it WorkBuddy uses PowerShell, which cannot run this command. The command is
`"<python>" -I -B -m scope_recall.adapters.codex.hook_entry --home "<instance-root>" --host workbuddy || exit 1`
with forward slashes, plus `--env-file "<file>"` before the `||`: keep those paths to printable ASCII without
`"`, `$`, `` ` `` or `\`; `apply-install` refuses others. WorkBuddy's `timeout` is in seconds,
and a prompt hook that runs past it blocks the prompt: the hooks wait 15 s (`UserPromptSubmit`)
and 10 s (`Stop`, `SessionEnd`), the interpreter's start plus the entry's
`hook_processing_seconds` (at most 6 s). A hook answers as soon as its work is done.

What the hooks do, as for the other clients: a prompt is stored as the owner's and what is
remembered is put in front of it; a `Stop` stores the reply and reads WorkBuddy's session record
(the hook's `transcript_path`, else the session's file under `<WorkBuddy home>\projects\`) from
where the last read stopped, for the text shown between tool calls and the owner's messages the
prompt hook could not store; `SessionEnd` reads the rest and forgets the session's turns. A turn
is named by the prompt's `generation_id` when it is new to the session, else by one derived from
the session, the words and the moment, kept in `<instance-root>\scope-recall\turns\` until the
session ends (a day at most). The MCP server serves the tools.

The prompt's recall comes from a resident recall server (from 3.6.0), which keeps the entry's
vector search and embedding connection warm. WorkBuddy 5.6.2 runs its MCP servers inside a
conversation's agent process: it starts one when a conversation opens or a prompt comes to a
conversation without one, and stops it at its own time. A server started there met the prompt
that started it still opening its vector store, and a cold server answered with its vector search
only 12.7 s after its start, past the hook's 6 s. The resident server is a process of its own,
started apart from the MCP server. It still lives only as long as the conversation's agent process
that started it: WorkBuddy's agent puts itself and every process it starts in a Windows job that
ends them all when the agent's process ends, and the server cannot leave that job (agent 2.147.0,
measured 2026-10-04). While one conversation's process runs, another conversation's first prompt
finds the server warm (1.48 s, with its vector search, measured the same day); the first
conversation after WorkBuddy starts, and a prompt right after the conversation holding the server
ended, are recalled without it (see Known limits):

- The prompt hook starts it after its answer when none runs, at most once a minute. The MCP
  server WorkBuddy runs with a conversation starts it too, and looks again every 30 s while that
  conversation's process runs. No task or service is registered with the system.
- It names itself in the entry's endpoint folder; the hook asks it before any other server.
- It ends `resident_recall_minutes` after the last prompt's recall or the last look of a running
  MCP server, whichever is later: 120 for WorkBuddy, set in the entry's runtime config (see
  [configuration.md](configuration.md)). It reads the value every 30 s; set to 0, it ends within
  30 s and none is started again.
- It ends within 30 s once its package on disk is replaced or removed; the next prompt, or a
  running MCP server, starts the new version's. A prompt hook that finds one of another version
  running (from another venv, say) stops it and starts its own, where it can prove the process is
  that server and may end it; no hook or MCP server keeps one of another version up, so one it
  cannot stop ends at its idle end. `apply-install` stops it as well. Hooks of two versions against
  one entry switch it at most once a minute: run a canary against a copy of the entry's home, not
  the live one.
- It ends once a recall has run 5 minutes past its time: such a server answers every hook that it
  is busy. The next look starts a new one.
- While it runs it holds a vector helper, about 1 GB. The MCP server WorkBuddy runs with each
  conversation keeps no helper warm of its own and answers no hook; a tool's vector search starts
  one in that server.
- The first prompt after it ended (with its conversation's process, or at its idle end), after
  WorkBuddy started or after a reboot, starts it and is recalled the old way, usually by words and
  the stored structure alone; the next prompts find it warm.
- It writes nothing to the store. One runs for each entry: a second of the same version gives way
  to the first.

Stop it before a `package-upgrade` of the entry's package, after quitting WorkBuddy (a running MCP
server starts it again): `scope-recall resident stop --home <instance-root> --host workbuddy`
(`status` shows it; `stop` exits 1 and says `still_running` when one still holds the lock). Where
a process's start time cannot be read (macOS), `stop` cannot tell the server from another process
that took its id and leaves it alone (`verified: false`); it ends itself within 30 s of an upgrade
in place, and at its idle end when the entry moved to another venv. `apply-install` and
`apply-uninstall` stop it too, where its identity is proven, and say on stderr one they could not. Until the MCP server is
approved, the hooks still store and recall, and start the resident server all the same.

To check it: `doctor --host workbuddy --instance-root <instance-root>` checks the binding and the
store (it does not read WorkBuddy's settings; `host_registration_status: pending` is healthy, as
for Codex). Then open a workspace in WorkBuddy (its hooks fire only there), send a message and
look for the entry in `scope-recall entries --root <store>` (last heard from) and for
`scope-recall` among WorkBuddy's connected MCP servers. WorkBuddy asks for the approval again
when the server's command, arguments or environment names change.

To take it out: quit WorkBuddy, run `plan-uninstall` and `apply-uninstall --instance-root
<instance-root>`. They take this entry's hooks and server out of WorkBuddy's two files
(`unmerged_files`; a copy of each goes to the backups first) and leave everything else; `detach`
then ends the entry. `detach` alone leaves WorkBuddy's settings as they are.

Known limits:

- The resident recall server ends with the conversation's agent process that started it, since
  WorkBuddy's agent ends every process it started. The first conversation's first prompt after
  WorkBuddy starts, a prompt right after the conversation holding the server ended, and the first
  prompt after the server's idle end or a reboot are recalled without it, usually by words and the
  stored structure alone. A conversation whose MCP server still runs starts a new one within 30 s,
  and no sooner than a minute after the last start; one whose MCP server WorkBuddy stopped starts
  it with its next prompt, which is recalled without it. A WorkBuddy on another machine is answered
  by its entry's server here, which runs on ([remote-entries.md](remote-entries.md)).
- A reply that is only an error WorkBuddy showed in place of one (not signed in, a model or network
  failure; its session record marks that message with the error) is not stored, from 3.6.2, nor is
  that error when a later stopped turn hands it to its `Stop` again. A WorkBuddy on another machine
  judges this from its own record and tells its entry's server, which never opens a record for a
  request. A reply that broke off with an error keeps what was shown.
- WorkBuddy hands the prompt hook a prompt with its line breaks removed, so a multi-line message
  is stored as one line.
- WorkBuddy fires `Stop` for a cancelled or failed turn too, with the previous turn's reply; a
  reply that repeats the session's last one is not stored by the hook. When the turn did say the
  same words again, the record read stores them from the session record.
- A user message in WorkBuddy's session record counts as the owner's only inside its
  `<user_query>` blocks; command and shell output, a teammate's report or a slash command's
  expansion there is not stored as the owner's words.
- Messages sent while a turn runs reach the next prompt hook as the last of them only. The others
  are stored from the session record when that turn ends, together with the last, which is so
  stored twice.
- A prompt WorkBuddy sends on its own, a session cron's or a goal's start, reaches the prompt hook
  as the owner's would and is stored as theirs. A background task's notice and a Stop hook's or a
  goal's request to go on (`Stop hook feedback:`) are skipped.
- The hook command ends in `|| exit 1`: WorkBuddy blocks a prompt whose hook exits 2, which an
  older package that does not know `--host workbuddy` would. Before rolling the package back below
  3.5.0, take the hooks out with `apply-uninstall`; an older package cannot.
- A subagent's work is not recorded: WorkBuddy fires no prompt or `Stop` hook for it, and its
  record is not read.
- Not done: `doctor` does not check WorkBuddy's settings, and the `scope-recall-memory` skill is
  not installed into WorkBuddy.

## 13. DeepSeek Harness (dsh)

dsh runs the hooks and the MCP server of the `codex` adapter as an entry of a shared store, the
owner at this machine (measured with dsh 0.2.0-rc.2). Its own hooks name no turn and no reply,
and its session log is compressed, so a hook alone cannot record a turn: a dsh plugin, which the
installer writes, runs the entry's hooks instead. Before the first step of each turn it runs the
prompt hook with the person's message and adds what is remembered to that step; it keeps the
turn's messages as dsh commits them and stores them when the turn ends. dsh's own MCP client runs
the MCP server, which serves the tools (`mcp__scope-recall__recall` and the rest).

Attach dsh's entry first ([shared-store.md](shared-store.md)), from an environment with the
`codex` extra, then install:

```powershell
$Entry  = "D:\ScopeRecall\dsh"
$Python = "D:\ScopeRecall\dsh-venv\Scripts\python.exe"

scope-recall plan-install --host dsh --instance-root $Entry `
  --agent-id <the store's agent id> --python $Python --env-file <the file with the embedding key>
# quit every dsh: web, tui, Desktop, and headless runs
scope-recall apply-install --host dsh --instance-root $Entry `
  --agent-id <the store's agent id> --python $Python --env-file <the file with the embedding key>
# start dsh
```

`--target-plugin-dir` names dsh's home; without it the installer uses `DSH_HOME`, else
`%USERPROFILE%\.dsh`, and refuses a home that does not exist (start dsh once). `apply-install`:

- writes the plugin to `<dsh home>\scope-recall\dsh-plugin\index.mjs` (in the receipt;
  `apply-uninstall` removes it);
- adds two rows to dsh's home patch, `<dsh home>\cordis.patch.yml`, as one `insert` between
  `# SCOPE_RECALL_DSH_START` and `# SCOPE_RECALL_DSH_END`: `scope-recall`, the plugin (named by its
  `file:///` URL, with the interpreter, the entry and the env file in its `config`), and
  `mcp-scope-recall`, dsh's MCP client running the stdio server `scope-recall`. dsh composes every
  profile with this file after the profile's own layers, so the rows reach `web`, `tui`,
  `headless` and any profile you made;
- switches off dsh's upload of its session logs. dsh sends each session's log to its model API by
  default (the row `session-log-deepseek`), and what is recalled is in that log. Unless the file
  already leaves it off, the install adds that row with `enabled: false`, after every other operation
  on it, between `# SCOPE_RECALL_DSH_PRIVACY_START` and `# SCOPE_RECALL_DSH_PRIVACY_END`. What leaves it
  off is worked out as dsh does (see below): the last `disabled` and the last `config` given for the
  row, a `config` without `enabled: false` switching the upload on again. An operation that switches
  it on again after the install's is followed by the install's own at the next install. Uninstall
  leaves the switch there: switched on again, the upload would send what was recorded while it was
  off;
- keeps every other line of the file, copies the file to
  `<instance-root>\.scope-recall-backups\<id>\plugin\` first, and changes nothing when run again;
- refuses, and writes nothing, when the file is not a YAML list written as a block at column 0,
  when something else inserts a row `scope-recall` or `mcp-scope-recall` or gives an MCP server the
  name `scope-recall`, or when it holds another Scope Recall entry's rows.

dsh applies the operations in the file in order, each key replacing the row's own: an operation of
yours after the block that names a row (`- id: scope-recall` with `disabled: true` switches the
plugin off) stays after it. A re-install writes the block where it stood, and an install after an
uninstall writes it before the first such operation. A `config` there replaces the row's whole
`config`. Edits inside the block are not kept.

To check it: `dsh headless --dump-config` (or the profile you use) prints the composed rows;
`scope-recall` and `mcp-scope-recall` are among them, and `session-log-deepseek` has
`enabled: false`. `doctor --host dsh --instance-root <instance-root>` checks the binding and the
store (it does not read dsh's files). Then send a message in dsh and look for the entry in
`scope-recall entries --root <store>` (last heard from) and at
`<instance-root>\scope-recall\dsh-plugin-status.json`.

dsh also sends a session to DeepSeek when you send feedback on it (`session-telemetry-otel`, which
does nothing else by default). Set the user environment variable `DSH_TELEMETRY_DISABLED=1` to
switch that off as well; the installer does not change your environment.

What the plugin does:

- Before the first step of each turn it runs the prompt hook with the person's message (the last
  of theirs that step takes): the prompt is stored as the owner's, named by the session and dsh's
  turn number, and what is remembered is added to the step as a message of its own
  (`source.kind: plugin:scope-recall`) after the person's. The step waits for the answer up to 9 s;
  past that, or when the hook fails, the turn goes on without it. Cancelling the turn ends the hook.
- It keeps each turn's messages as dsh commits them, the text of the person's and of the model's, in
  `<instance-root>\scope-recall\dsh-spool\`, one file per session and dsh process. When the turn ends
  it runs the `Stop` hook with them: the reply under the turn, what the model said while it worked,
  and what the person sent meanwhile. The hook answers how many it stored, and those leave the file.
  A `Stop` stores what it can in its time (it reads at most 3 s of messages); the next takes the rest
  at once, and only one that stores nothing ends the store.
- What a failed store left is stored at the session's next turn end, or by a pass that runs every
  minute over files idle for 2 minutes, one session at a time: a store that stores nothing ends a
  pass, and the next waits longer, up to 30 minutes. A dsh that quits waits up to 3 s for a store that is
  running; what it left is taken up by the next dsh that runs, once that process is gone (or the
  file has been idle for 6 hours). A message sent twice is stored once.
- A completed turn's reply goes with its `Stop` when it was said at most a minute before: the store
  recognises it among the turn's messages by its words and a moment 120 s away at most, and the
  reply's moment is the hook's. A reply said earlier (before a long tool call that ended the turn,
  say), and the last words of a turn that did not complete, are stored from the turn's messages alone,
  as the model's words.
- Bounds: a message's text is kept up to 20,000 characters (and 36 KB), with a marker for the rest;
  a message waiting longer than 14 days, or past 5,000 waiting in a session, is dropped and said in
  dsh's log.
- `dsh-plugin-status.json` shows the last recall and store, how many messages wait, how many were
  dropped, when the next pass may run, and a privacy alarm: should dsh report a session log
  delivered to its API, the plugin says so there and in dsh's log. A hook that fails (an interpreter
  that cannot import the package, say) is shown there with the end of its stderr.
- A subagent's session is neither recalled for nor recorded. Tool calls and results, files and
  images, and the model's reasoning are not recorded.

The prompt's recall comes from the entry's resident recall server (section 12), which keeps the
vector search warm from one dsh process to the next: the prompt hook starts it after its answer
when none runs, at most once a minute, and so does the MCP server dsh runs, which looks again every
30 s. It ends `resident_recall_minutes` after the last prompt's recall or the last look of a running
MCP server, 120 by default for dsh ([configuration.md](configuration.md)). The first prompt after
it ended, or after a reboot, is recalled without it, usually by words and the stored structure
alone. Stop it before a `package-upgrade` of the entry's package, after quitting dsh:
`scope-recall resident stop --home <instance-root> --host dsh`.

To take it out: quit dsh, then `plan-uninstall` and `apply-uninstall --instance-root
<instance-root>`. They remove the plugin file and this entry's rows (`unmerged_files`; a copy goes
to the backups first) and leave the upload switched off; `detach` then ends the entry. Messages
still waiting in the spool stay there unstored.

Known limits:

- Local only: a dsh on another machine cannot reach its entry over HTTP yet
  ([remote-entries.md](remote-entries.md) covers Claude Code, Codex and WorkBuddy).
- dsh 0.2.0-rc.2 is a candidate; the plugin relies on its plugin interface (`agent/pre-step`,
  `session/event`, the session format V4). A dsh that changes it may run the turn without the
  plugin; `dsh-plugin-status.json` then stops changing.
- What is recalled stays in the session as a message, as it does in the other clients' sessions.
- `doctor` does not read dsh's patch file, and the `scope-recall-memory` skill is not installed into
  dsh.

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
| WorkBuddy home | `WORKBUDDY_CONFIG_DIR`, else `%USERPROFILE%\.workbuddy` |
| WorkBuddy hooks and MCP server | `hooks` in `<WorkBuddy home>\settings.json`, `mcpServers.scope-recall` in `<WorkBuddy home>\mcp.json` |
| dsh home | `DSH_HOME`, else `%USERPROFILE%\.dsh` |
| dsh plugin and rows | `<dsh home>\scope-recall\dsh-plugin\index.mjs`; rows `scope-recall` and `mcp-scope-recall` in `<dsh home>\cordis.patch.yml` |
| dsh spool and status | `<instance-root>\scope-recall\dsh-spool\`, `<instance-root>\scope-recall\dsh-plugin-status.json` |
