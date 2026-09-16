# Scope Recall 3.1 autonomous memory candidate

Scope Recall v3 is a bounded local memory core with SQLite as the authority and rebuildable vector companions. It provides host adapters for Hermes and Codex, including Codex MCP tools when the optional `codex` extra is installed. The public package is `hermes-scope-recall`; the Python import is `scope_recall`; the host wrapper identity remains `scope-recall`.

This checkout is release candidate `3.1.0rc29`. It bounds the reads that were
silently truncating -- embedding input, evidence budget, packet slots -- and
reports what each one cut; it collapses byte-identical retrieved content so a
packet slot is never spent saying the same thing twice; and it adds operator
commands to re-judge claims and to clear failures a shipped fix has cured.
SQLite remains the only fact authority; host adapters share the same contracts.
This is a local candidate, not a published release or a claim of live deployment.

**Why it is a candidate and not a release.** `scripts/check.py --tier release`
runs 900 tests with none failing, but exits 2 and reports
`missing_gates: ["model"]`. That gate does not clear by making model calls: it
wants a P18 formal acceptance receipt, which requires denominators of 120
independent core items and 240 paired variants, evidence marked `real`, a method
adjudication accepted by a party independent of whoever wrote the code, and an
independent semantic scorer. The P18 machinery is in this tree; the evaluation
corpus is not -- `tests/eval/public_fixture.jsonl` holds two rows, both flagged
`"simulation": true`. Read a green test count here as exactly that, and never as
a passing release gate: reporting the count without the exit code is how that
mistake was made before. Integration is 1223/0 and packaging 114/0, both exit 0.

## For agents: install or upgrade on the user's behalf

Users only need to ask "install Scope Recall" or "帮我升级一下 scoperecall".
Start with `scope-recall setup --host <hermes-or-codex> --home <actual-instance-home>`.
New users go directly to installation; only detected legacy databases go through
[the agent migration workflow](maintenance/AGENT_WORKFLOW.md). The installed
`scope-recall-setup` skill makes this routing discoverable in both supported hosts.
Perform path discovery, backup, audience binding, migration, indexing and host
checks yourself; do not ask the user to execute commands or govern old memories.

## Install the current candidate

Step-by-step Hermes and Codex instructions: [docs/install.md](docs/install.md).

Build the local candidate first, then install that wheel into the same isolated
Python environment used by the host. The package is not published to PyPI:

```text
python -m build --wheel
python -m pip install "<absolute-path-to-wheel>"
python -m pip install "<absolute-path-to-wheel>[codex]"
```

The current release candidate is `3.1.0rc29`; these commands are local
placeholders until a reviewed wheel is built. They do not claim that full host
runtime wiring or production registration has been accepted.

The two console names `scope-recall` and `hermes-scope-recall` invoke the same v3 maintenance CLI. They are aliases for the current CLI only; neither is a compatibility promise for an older command set.

Use explicit absolute paths for installation planning. Hermes `--agent-id` must match the host active profile (`get_active_profile_name()`, commonly `default` on an isolated home). Hermes default `--agent-workspace` is `hermes` to match the host memory-provider init contract; pass the same value on plan and apply if you override it. Codex does not accept `--agent-workspace`.

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

This candidate adds two shared read-only Core methods, `profile` and `entity`, and exposes them on both Hermes tools and Codex MCP. They return a deterministic categorized current-fact view, or an exact one-hop statement view, over admitted consolidated claims only. Raw chat is never silently turned into a profile. Incoming relations match the full scalar `value_text` only and keep recorded conditions and validity. Explicit project-name aliases may resolve when they are already admitted and still live; person aliases are not generalized. A `budget_tokens` value too small for even the minimal truthful envelope is a validation error, not an oversized view. See [docs/profile-entity.zh-CN.md](docs/profile-entity.zh-CN.md). These tools exist on this candidate checkout; an older installed wheel does not gain them until that candidate is reviewed, packaged, and installed.

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
