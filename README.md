# Scope Recall 3.1 autonomous memory

Scope Recall v3 is a bounded local memory core with SQLite as the authority and rebuildable vector companions. It provides host adapters for Hermes and Codex, including Codex MCP tools when the optional `codex` extra is installed. The public package is `hermes-scope-recall`; the Python import is `scope_recall`; the host wrapper identity remains `scope-recall`.

This is `3.1.2`, the second maintenance release of the 3.1 line: `3.1.1` was what running
`3.1.0` on real instances turned up, fixed, and `3.1.2` is what rolling `3.1.1` out turned up.
Their notes are the `[3.1.2]` and `[3.1.1]` sections of [CHANGELOG.md](CHANGELOG.md); upgrading
from `3.1.0` or `3.1.1` is `pip install -U` and a host restart.
3.1 is a rebuild rather than a patch on 2.0: production
code went from 141,044 lines to 48,289, memory now accumulates evidence before a
fact is written rather than judging one sentence on sight, and hosts sit behind
adapters instead of the core being shaped around Hermes. The notes for the rebuild are
the `[3.1.0]` section of [CHANGELOG.md](CHANGELOG.md), and section 9 there is the
migration procedure for a 2.0.1 memory database. SQLite remains the only fact
authority; host adapters share the same contracts.

**What is not verified.** `scripts/check.py --tier release` runs about 2,200 tests with
none failing, but reports `missing_gates: ["model"]`. That gate wants a P18
formal acceptance receipt: denominators of 120 independent core items and 240
paired variants, evidence marked `real`, a method adjudication accepted by a
party independent of whoever wrote the code, and an independent semantic scorer.
The P18 machinery is in this tree; the evaluation corpus is not. **3.1.0 shipped
without that receipt and so does 3.1.1.** Every accuracy figure in the notes was measured by us, on
our own corpora, by hand, and there is no regression suite you or we can re-run
automatically -- that is the first item in *What is not finished*. Read a green
test count as exactly that, never as a passing release gate. The integration
(about 2,060 tests) and packaging (136) tiers both exit 0 with none failing.

## For agents: install or upgrade on the user's behalf

Users only need to ask "install Scope Recall" or "帮我升级一下 scoperecall".
Start with `scope-recall setup --host <hermes-or-codex> --home <actual-instance-home>`.
New users go directly to installation; only detected legacy databases go through
[the agent migration workflow](maintenance/AGENT_WORKFLOW.md). The installed
`scope-recall-setup` skill makes this routing discoverable in both supported hosts.
A second installed skill, `scope-recall-memory`, is for the owner's everyday questions:
what is remembered about me, did I say it or was it worked out, does it still hold, and
what correcting, muting or deleting one memory does before it is done.
Perform path discovery, backup, audience binding, migration, indexing and host
checks yourself; do not ask the user to execute commands or govern old memories.

## Install

Step-by-step Hermes and Codex instructions: [docs/install.md](docs/install.md).

The package is `hermes-scope-recall` on PyPI. Install it into the same isolated Python
environment the host uses:

```text
python -m pip install hermes-scope-recall==3.1.1
python -m pip install "hermes-scope-recall[codex]==3.1.1"
```

The same wheel and sdist are attached to the
[GitHub Release](https://github.com/410979729/scope-recall-hermes/releases/tag/v3.1.1)
alongside `SHA256SUMS` and `RELEASE-PROVENANCE.json`, for an offline install
(`python -m pip install "<path-to-wheel>"`). To build it yourself from this checkout instead:

```text
python -m build --wheel
```

Upgrading from 2.0.x is not an in-place upgrade. Read section 9 of
[CHANGELOG.md](CHANGELOG.md) before you start; your old database needs a
one-time offline migration and there are two errors people commonly hit.

The two console names `scope-recall` and `hermes-scope-recall` invoke the same v3 maintenance CLI. They are aliases for the current CLI only; neither is a compatibility promise for an older command set.

Use explicit absolute paths for installation planning. Hermes `--agent-id` must match the host active profile (`get_active_profile_name()`, commonly `default` on an isolated home). Hermes default `--agent-workspace` is `hermes` to match the host memory-provider init contract; pass the same value on plan and apply if you override it. Codex does not accept `--agent-workspace`.

If you talk to Hermes through the Desktop app or `hermes --tui` rather than the CLI, add `--local-platform desktop` (or `tui`) to both commands. Those surfaces name no user unless a dashboard login exists, and a session that names no user is refused everywhere but the CLI until the installer approves the surface; [docs/install.md](docs/install.md) says what the approval does and does not cover.

```text
scope-recall plan-install --host hermes --target-plugin-dir <absolute-plugin-dir> --instance-root <absolute-instance-root> --project-root <absolute-project-root> --agent-id <agent-id> --python <absolute-python>
scope-recall apply-install --host hermes --target-plugin-dir <absolute-plugin-dir> --instance-root <absolute-instance-root> --project-root <absolute-project-root> --agent-id <agent-id> --python <absolute-python>
scope-recall doctor --host hermes --instance-root <absolute-instance-root> --python <absolute-python>
```

Codex uses the same commands with `--host codex`, plus `--env-file <absolute-file>` so the Codex-launched MCP server and hooks can read the credential names the runtime config declares (Codex does not pass them in the environment). The installer writes only its own wrapper and installation records and preserves foreign host files. Its receipt records host registration and applicable hook trust as pending at installation time; use doctor and actual host loading to verify the current state. Uninstall keeps Core data by default; explicit purge is bounded to verified installation-owned data and refuses uncertain ownership or active writers.

## Uninstall (default: retain memory)

Uninstall is receipt-driven. By default it removes only plugin wrapper files and retains Core data. Inspect the plan before applying:

```text
scope-recall plan-uninstall --instance-root <absolute-instance-root>
scope-recall apply-uninstall --instance-root <absolute-instance-root>
```

`--target-plugin-dir` may be omitted when the install receipt records it. Purge of installation-owned Core data requires a separate `plan-uninstall --purge` inspection and matching `apply-uninstall --purge`; see [docs/install.md](docs/install.md). Do not treat purge as a normal uninstall step.

## Data and host boundaries

SQLite truth lives in the verified Core data directory. Vector indexes are companions and may be rebuilt only through an explicit configured space. The Hermes and Codex adapters bind host identity, installation identity, scopes, and project roots before opening Core. After that binding is verified, an omitted runtime-config argument checks only `<data_directory>/runtime-config.json`; a missing file stays basic plus an explicit capability gap. The file is never generated or discovered from the current directory, parent directories, or credential locations. Optional host wiring must report a capability gap instead of creating or repairing a database.

The `codex` extra adds the MCP SDK. Without that extra, the Core and Hermes paths remain importable. The `lancedb` extra enables the tested LanceDB companion path. PostgreSQL/pgvector is outside this v3 distribution; an explicit configuration error directs operators to retain the old installation and use the migration guide.

On Windows, choose a short data directory such as `C:\ScopeRecall\my-agent`. LanceDB appends index, table, and temporary file names to that path. If the resulting native path is too long, the worker reports `native_vector_path_too_long` before starting LanceDB or calling the embedding API. Use a short target directory for a new installation or an offline migration.

## Profile and entity read views

3.1.0 adds two shared read-only Core methods, `profile` and `entity`, and exposes them on both Hermes tools and Codex MCP. They return a deterministic categorized current-fact view, or an exact one-hop statement view, over admitted consolidated claims only. Raw chat is never silently turned into a profile. Incoming relations match the full scalar `value_text` only and keep recorded conditions and validity. Explicit project-name aliases may resolve when they are already admitted and still live; person aliases are not generalized. A `budget_tokens` value too small for even the minimal truthful envelope is a validation error, not an oversized view. See [docs/profile-entity.zh-CN.md](docs/profile-entity.zh-CN.md). An older installed wheel does not gain these tools until 3.1.0 is installed.

## Bounded multi-hop evidence paths

Use `trace` when a question needs two or three recorded relationships joined
together. It is read-only, shared by Hermes and Codex, and reuses existing SQLite
fact/evidence/visibility checks. It does not invoke another model or persist
inferred facts. Cross-scope names and conditional relations are not silently
joined. Node, path, time and explicit byte budgets bound its cost. See
[the trace contract and boundaries](docs/trace.zh-CN.md).

## Agent-operated migration

Users ask their agent to upgrade. The bundled `scope-recall-setup` skill routes
fresh installs directly to installation and existing Core databases to ordinary
updates. Only legacy SQLite uses a durable migration job. The agent reads
`scope-recall setup --workflow` and performs discovery, verified audience mapping,
backup, conversion, indexing and actual host checks. See
[the agent upgrade guide](docs/upgrade-guide.zh-CN.md).

Migration composes the existing converter, backup helper and worker. It preserves
original source/history/deletion semantics and never re-extracts the whole old
journal. Unsupported formats or unresolved permissions block cutover and preserve
the old installation. Index scheduling and actual live readiness remain separate.

## Tree layout

The package root holds only the entry (`__init__.py`), the version and the protocol contracts. `core/` is the host-independent memory core over SQLite truth; `vector/` the rebuildable vector companions; `adapters/` the Hermes and Codex host adapters and model transport; `runtime/` the background worker, budgets and scheduling; `maintenance/` install, doctor, upgrade and migration behind the operator CLI. Every shipped module is reachable by import from an entry point named in `packaging_hooks/module_inventory.py`; the wheel allowlist is derived from that, not typed.

## Development checks

The clean wheel must be tested outside the source checkout. At minimum, verify both CLI aliases, a read-only doctor result, Core capture and recall against a temporary installation, and the stdlib HTTP helper's bounded invalid-input response. Host registration, real gateway lifecycle, and production data are separate acceptance boundaries.

Historical release notes and the former v2 packaging contract remain available in the repository history at the `v2.0.1` tag. They are not current v3 usage instructions.
