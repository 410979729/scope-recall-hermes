# Hermes upstream recommendation plan

`scope-recall` is a standalone Hermes memory-provider plugin. The immediate goal is **official/recommended standalone provider visibility** in Hermes documentation or plugin discovery, not an in-tree `plugins/memory/` merge.

Hermes upstream policy currently says the built-in memory-provider set is closed. The maintainable path is therefore:

1. make `scope-recall` excellent as a standalone plugin;
2. ask maintainers for a docs/featured/recommended-provider path;
3. submit the smallest upstream PR that improves Hermes users' ability to discover/install standalone memory providers;
4. only discuss in-tree bundling if maintainers explicitly reopen that policy.

## Upstream request shape

Preferred first request:

> Please consider listing `scope-recall` as a standalone local-first Hermes memory provider, or accepting a docs PR that adds a “Standalone memory providers” section with `scope-recall` as an example/community provider.

Do **not** open a first PR that adds a new `plugins/memory/scope-recall/` directory to `NousResearch/hermes-agent`. That contradicts current maintainer policy and is likely to be closed before the plugin merits are evaluated.

## Product positioning

Public one-line description:

> `scope-recall` is a local-first Hermes memory provider with current-turn recall, scoped durable memory, SQLite truth storage, and rebuildable semantic companion indexes.

Primary differentiators:

- local-first and inspectable: SQLite is the authoritative truth store;
- vector indexes are rebuildable companions, not storage authority;
- durable `user` / `memory` / `project` / `ops` facts can follow the same user + agent identity across windows/chats;
- raw `general` scratch stays local to the current runtime scope;
- current-turn recall avoids stale previous-topic injection;
- Hermes curated `USER.md` / `MEMORY.md` files stay authoritative and are live-read instead of mirrored;
- operator tools expose inspect/search/feedback/export/repair/governance without requiring a hosted SaaS account;
- degraded operation remains available through SQLite lexical recall even when vector dependencies are unavailable.

## Upstream readiness status

Status after the `v1.1.0` installer/distribution work. Completed items must still be re-verified immediately before tag/release/upstream PR, but this is the target evidence shape for proposed standalone-provider visibility.

### Repository hygiene

- [ ] Current follow-up branch working tree is clean before merge/release.
- [ ] Latest git tag agrees with `pyproject.toml`, `plugin.yaml`, README version text, and `CHANGELOG.md` after the `v1.1.0` release tag is created.
- [x] `python -m pytest -q` passes locally.
- [x] `python scripts/check.release.py` passes locally.
- [x] CI passes on Python 3.11 and 3.12.
- [x] Release docs document the supported standalone install shape: Python distribution `hermes-scope-recall`, provider ID `scope-recall`, unpacked provider directory `$HERMES_HOME/plugins/scope-recall/`.
- [x] Clean-venv installer smoke verifies both native-free and `[lancedb]` installs can run `hermes-scope-recall install` and `verify`.
- [x] Hermes memory-provider discovery smoke verifies `discover_memory_providers()` and `load_memory_provider("scope-recall")` work against the clean-installed provider directory.

### Public issue closure

- [x] Issue #4 naming contract is documented in the repo and the issue is updated/closed with a maintainer summary.
- [x] Issue #1 non-AVX compatibility is resolved with the `sqlite-bruteforce` backend.
- [ ] PR #2 MiniMax embedder is either merged after release-gate fixes or explicitly superseded by a maintainer implementation.

### Compatibility and install polish

- [x] README quick start includes a copy/paste clean-profile smoke test.
- [x] README explains the difference between `scope-recall` and `scope_recall`.
- [x] Vector backend config documents `lancedb` and non-AVX fallback behavior.
- [x] `vector_store.py` does not force native `pyarrow` import when LanceDB is not selected.
- [x] A non-AVX-safe backend such as `sqlite-bruteforce` exists, or vector import failures degrade cleanly without native import crashes.
- [x] `scripts/doctor.py` reports backend availability and actionable next steps.

### Upstream materials

- [x] Draft GitHub Discussion / RFC for `NousResearch/hermes-agent`.
- [ ] Draft docs-only PR text for a “Standalone memory providers” section.
- [x] Prepare an evidence bundle: install command, activation command, smoke-test output, tests, release gate, known limitations, maintenance commitment.

### Remaining follow-up

- Keep PR #2 (`MiniMax` embedder) blocked until the contributor rebases and resolves review comments, or supersede it with a maintainer branch.
- Re-run `ruff check .`, `python -m pytest -q`, and `python scripts/check.release.py` before any new tag or upstream docs PR.
- After upstream maintainer feedback on the RFC, prepare the smallest docs/plugin-discovery PR they are likely to accept.

## Suggested implementation order

1. Land documentation contract for naming and upstream recommendation path.
2. Implement `sqlite-bruteforce` vector companion backend with tests.
3. Update README/config/stability docs for backend choices and non-AVX operation.
4. Resolve or supersede #4 and #1 publicly.
5. Re-evaluate PR #2 after its author responds; if they do not, implement MiniMax ourselves in a separate maintainer branch without blocking compatibility work.
6. Run full release gate and CI-equivalent local checks.
7. Open Hermes upstream Discussion/RFC.
8. After maintainer response, submit the smallest docs/plugin-discovery PR they are likely to accept.

## Upstream RFC draft skeleton

```md
# RFC: list scope-recall as a standalone local-first Hermes memory provider

## Summary

`scope-recall` is a standalone Hermes memory provider focused on current-turn recall, scoped durable memory, SQLite truth storage, and rebuildable semantic companion indexes.

We understand Hermes' current policy that new in-tree memory providers under `plugins/memory/` are closed. We are not asking to add a new in-tree provider in this first request. Instead, we would like to ask whether maintainers would accept a docs/featured-provider path for standalone memory providers.

## Why this fills a gap

- local-first; no hosted memory account required;
- SQLite truth store remains inspectable and authoritative;
- vector backend is optional/rebuildable;
- durable facts and local scratch are separated by runtime scope;
- Hermes curated memory files remain authoritative and live-read;
- explicit tools exist for store/search/context/probe/feedback/forget/export/inspect/explain/benchmark.

## Install and activation

```bash
python -m pip install "hermes-scope-recall[lancedb]"
hermes-scope-recall install --hermes-home "${HERMES_HOME:-$HOME/.hermes}"
hermes config set memory.provider scope-recall
hermes memory setup
hermes memory status
```

Native-free / no-AVX path:

```bash
python -m pip install hermes-scope-recall
hermes-scope-recall install --hermes-home "${HERMES_HOME:-$HOME/.hermes}"
# set vector.backend=sqlite-bruteforce in $HERMES_HOME/scope-recall/config.json when needed
hermes memory status
```

## Request

Would maintainers be open to one of:

1. a docs-only PR listing `scope-recall` under standalone/community memory providers;
2. a broader “Standalone memory providers” page;
3. a small plugin-discovery/install UX improvement for third-party memory providers?
```

## Non-goals

- Do not claim built-in/in-tree status unless Hermes maintainers explicitly grant it.
- Do not claim direct wheel-only Hermes discovery while current Hermes memory discovery remains directory-based.
- Do not claim drop-in compatibility with OpenClaw `.lance` stores; migration is explicit import/transformation into SQLite truth.
- Do not make LanceDB the truth store.
