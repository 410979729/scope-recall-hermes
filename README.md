# Scope Recall

This checkout is `4.0.3`. Local, source-backed memory shared by Claude Code, Codex, Hermes, WorkBuddy and DeepSeek Harness.
SQLite keeps the original conversations; model-derived facts and vectors remain traceable and rebuildable.

## Quick start

1. In the Python environment you want the hooks to use, run `python -m pip install -U "hermes-scope-recall[codex]"`.
2. Run `scope-recall setup`. Review the detected hosts, store, scopes, zero model budget and worker plan, then confirm.
3. Start a new host session. Approve the plugin hooks or MCP server when the host asks.

For this development checkout, install from the repository root with `python -m pip install -U ".[codex]"`;
the package-index command above selects a published release. An unpublished wheel can be installed directly with its `codex` extra.

| Host | Setup and host-specific approval |
|---|---|
| Claude Code | [Setup](docs/install/claude-code.md) |
| Codex | [Setup and hook review](docs/install/codex.md) |
| Hermes | [Setup and local identity](docs/install/hermes.md) |
| WorkBuddy | [Setup and MCP approval](docs/install/workbuddy.md) |
| DeepSeek Harness | [Setup and plugin check](docs/install/dsh.md) |

Without model routes or budget, setup reports **原文检索可用** (original-text search available).
Use `scope-recall setup --configure-models` for guided routes and budget configuration, or
`--models <route-file.json>` for automation. Successful model checks report **完整记忆功能可用**
(full memory features available). Model keys are environment references; setup never asks for a key value.
[Installation](docs/install.md) covers shared stores, reruns, worker commands and verification boundaries.
For a shared store on a Linux Docker host, see [Docker deployment](docs/docker.md).
Agents can [hand off a task](docs/host-integration.md#task-handoffs) across entries, with one receiving entry and a bounded session-start report.

Automatic recall injects zero to six items that clear a lexical coverage or
vector similarity floor. Background preferences and tasks accompany passing
query evidence; a session's first resume request can recover its current task.
Acknowledgments, approvals, praise, bare continuation and session status prompts
within an existing session start no search or query embedding. Explicit recall
keeps its admission behavior. Retrieval reserves time for compilation and fresh
SQLite release checks, so slow optional channels can leave a partial packet with
their cutoff reason. Unverified items never ship. The thresholds and diagnostic counts are described
in [the recall configuration](docs/configuration.md#vector_threshold).

Current and automatic recall preserve the ordinary complete items and can
append a neutral list when remaining matching records span at least three
sessions. Each session contributes its highest-ranked remaining visible
record. The list does not assert that records describe the same event or
count how often something occurred. Every row labels its excerpt, full text
length, source time and versioned reference; use inspect to expand the source.
At most twelve rows share at most 1024 budget units and one quarter of the
packet budget, using only space left after the complete items. Rows that do
not fit are omitted without displacing full evidence. Visibility, privacy,
suppression, current versions and retelling checks apply before publication.
Historical reads keep their ordinary items. Existing packet fields and the
shared host renderer carry the list; no migration or configuration change is
required.

**Recall-quality checks.** The [synthetic recall benchmark](CONTRIBUTING.md#the-recall-benchmark)
provides reproducible development and held-out splits for retrieval, answerability,
scope isolation, packet size and latency. It uses proposed claims and a word-hashing
embedder without a model or network, so it does not establish consolidation quality,
semantic embedding quality or performance on private conversations. The test tiers
also check contracts, storage and host integration.

Extraction can normalize fact wording while keeping exact source character intervals. A separate model judgement handles support that literal checks cannot establish; unavailable or uncertain support remains a labelled candidate. [Claim contracts](docs/claim-mutation-contract.md) describe promotion, merging and source authority.

## Manage your memories

Use `scope-recall ui` with the same store selection to open the [local management page](docs/ui.md).
It provides Chinese and English browsing, reviewed actions, held-content review, private rules, export and backup status in your browser.
Capture screens text for embedded instructions, exfiltration links and hidden Unicode before indexing. Held sources
are excluded from recall and processing; explicit inspection returns a reason without their words. The owner can
review and release them on the [local management page](docs/ui.md). External and unattributed sources await an explicit review result.

Use `scope-recall memory` to browse, search, inspect history, correct a fact, mute it or delete a conversation.
It runs as the local store owner and makes no model request. Deletion shows the dependent sources and facts before
`--yes` applies it. `scope-recall memory export --out <new directory or .zip>` writes the visible store as a portable
package with original sources, facts, history, episodes, readable Markdown and optional retained attachments.
`scope-recall memory import --source <kind> --from <path> --source-id <stable account>` previews external
memory files and conversation exports without opening the store. Review the original actors, projects and time range,
then repeat with the same store selection, `--yes --preview-id <shown hash>`. Approval permits import; it does not
confirm the content as fact. [Importing memories](docs/import.md) describes supported formats and retry behavior.
`scope-recall memory secrets` lists stored credential matches by reference, provenance, time and pattern names.
After upgrading, review its findings, remove them with `scope-recall memory forget <refs>` and change any credential found.
[The management guide](docs/manage.md) covers store selection, filters, version checks and receipts; the
[export guide](docs/export.md) covers package layout, hashes and filtering.

Use `scope-recall private on` with one of `--session <id>`, `--project <path>` or `--all` to stop keeping a session,
directory or the whole store. `--mode full` refuses capture, recall and background model work;
`--mode read-only` refuses capture while recall continues; `--all --for 2h` sets a duration and `off`
closes a rule. Rules are stored as metadata only, overlap takes the stricter mode, and an event is
refused by its own occurrence time even when a later retry or transcript read arrives after the
rule ends. Undated events are refused when earlier private history could cover them. Directory
identity follows junctions and symbolic links on the owning machine; recalls and outbound
requests check again before release. [The private-mode guide](docs/private-mode.md) lists every host path, injection notice,
migration and what is stored or sent in each mode.

## Install or upgrade with an assistant

Run `scope-recall setup --dry-run --plan-json` to inspect the complete plan, then
`scope-recall setup --yes` after the owner has approved it. Existing installations reuse their store and grants.
Legacy conversion and live process coordination use the [migration workflow](maintenance/AGENT_WORKFLOW.md).

## Uninstall (default: retain memory)

Uninstall is receipt-driven. By default it removes only plugin wrapper files and retains Core data. Inspect the plan before applying:

```text
scope-recall plan-uninstall --instance-root <absolute-instance-root>
scope-recall apply-uninstall --instance-root <absolute-instance-root>
```

`--target-plugin-dir` may be omitted when the install receipt records it. Purge of installation-owned Core data requires a separate `plan-uninstall --purge` inspection and matching `apply-uninstall --purge`; see [docs/install.md](docs/install.md). Do not treat purge as a normal uninstall step.

## Data and host boundaries

SQLite truth lives in the verified Core data directory. Vector indexes are companions and may be rebuilt only through an explicit configured space. The Hermes and Codex adapters bind host identity, installation identity, scopes, and project roots before opening Core. After that binding is verified, an omitted runtime-config argument checks only `<data_directory>/runtime-config.json`; a missing file stays basic plus an explicit capability gap. The file is never generated or discovered from the current directory, parent directories, or credential locations. Optional host wiring must report a capability gap instead of creating or repairing a database.

The `codex` extra adds the MCP SDK. NumPy provides mapped retrieval and exact full-vector
scoring without SQLite extensions. The `lancedb` extra is only a reader for existing
LanceDB embeddings. Legacy `vector.backend` values map to the SQLite companion automatically.
The worker imports existing vectors without embedding calls and retains the original files.
Until all visible identities are verified, recall uses lexical channels and reports an import gap.
Missing or unreadable input remains a reported gap. Changing the embedding model still requires
an explicit re-embedding run; opening a store never authorizes paid reconstruction.

## Profile and entity read views

Two shared read-only Core methods, `profile` and `entity`, are exposed as Hermes tools and on Codex MCP. They return a deterministic categorized current-fact view, or an exact one-hop statement view, over admitted consolidated claims only. Raw chat is never silently turned into a profile. Incoming relations match the full scalar `value_text` only and keep recorded conditions and validity. Admitted aliases can identify people, objects, places, organizations and projects. A worker compares bounded spelling/vector candidates with source evidence before recording a reversible identity relation. Different scopes, projects and branches remain separate; ambiguous names stay unresolved. Entity, trace and recall recheck these relations when reading. A `budget_tokens` value too small for even the minimal truthful envelope is a validation error, not an oversized view. See [docs/profile-entity.zh-CN.md](docs/profile-entity.zh-CN.md).

## Bounded multi-hop evidence paths

Use `trace` when a question needs two or three recorded relationships joined
together. It is read-only, shared by Hermes and Codex, and reuses existing SQLite
fact/evidence/visibility checks. It does not invoke another model or persist
inferred facts. Cross-scope names and conditional relations are not silently
joined. Node, path, time and explicit byte budgets bound its cost. See
[the trace contract and boundaries](docs/trace.zh-CN.md).

## One store for several agents

Several agents can share one store instead of each keeping its own: each attaches as an
entry, every memory is marked with the agent it came in through, and a deletion through
any of them applies to all. Moving the memory to another machine is copying one directory.
Hermes homes attach as entries, and so do Codex and Claude Code
(`attach --host codex|claude-code`). See [docs/shared-store.md](docs/shared-store.md).
WorkBuddy and dsh attach the same way (`attach --host workbuddy|dsh`). Full backup bundles
and disaster recovery restore to fresh destinations are in [docs/backup-restore.md](docs/backup-restore.md).

## Agent-operated migration

Users ask their agent to upgrade. The bundled `scope-recall-setup` skill routes
fresh installs directly to installation and existing Core databases to ordinary
updates. Only legacy SQLite uses a durable migration job. The operator follows the [migration workflow](maintenance/AGENT_WORKFLOW.md) for verified audience mapping,
backup, conversion, indexing and actual host checks. See
[the agent upgrade guide](docs/upgrade-guide.zh-CN.md).

Migration composes the existing converter, backup helper and worker. It preserves
original source/history/deletion semantics and never re-extracts the whole old
journal. Unsupported formats or unresolved permissions block cutover and preserve
the old installation. Index scheduling and actual live readiness remain separate.

## Tree layout

The package root holds only the entry (`__init__.py`), the version and the protocol contracts. `core/` is the host-independent memory core over SQLite truth; `vector/` the rebuildable vector companions; `adapters/` the Hermes plugin, the clients that use hooks and an MCP server (Codex, Claude Code, WorkBuddy, dsh; `adapters/clients`, with the entry module names installed configurations run kept in `adapters/codex`) and model transport; `runtime/` the background worker, budgets and scheduling; `maintenance/` install, doctor, upgrade and migration behind the operator CLI. Every shipped module is reachable by import from an entry point named in `packaging_hooks/module_inventory.py`; the wheel allowlist is derived from that, not typed.

## Development checks

[Contributor checks](CONTRIBUTING.md) cover clean-wheel installation, both CLI aliases, read-only diagnostics and
capture/recall in temporary stores. These checks do not establish that a running host loaded the package.

Queue states, retry limits and recovery actions are described in [Background work](docs/background-work.md).
