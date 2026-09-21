# Scope Recall maintenance rules

Maintainability is a product requirement. Prefer a small, explicit change to a new framework.

- SQLite is the sole authority for facts, identity, permissions, lifecycle and evidence. Derived indexes are optional and rebuildable.
- Put behavior in one owning module. Host adapters translate requests and bind trusted identity; they do not implement another copy of Core policy.
- Extend or compose an existing implementation before adding another engine. Remove superseded production paths in the same change; preserve historical fixtures outside the shipping allowlist.
- Keep migration orchestration separate from legacy conversion, host activation and background indexing. A host integration must explicitly prove its writer/activation boundary.
- Multi-hop queries are read-only evidence joins. They must not infer identity, broaden audiences, resurrect invalid facts or promote query-time conclusions.
- Long work needs bounded stages, durable progress, idempotent retry and visible failure. Never turn unknown data into a successful migration or retry forever.
- A new public capability needs a shared contract, both applicable host surfaces, concise operator documentation and focused boundary tests. Avoid full-suite repetition without a specific reason.
- Do not add a dependency or an extensibility layer solely for hypothetical future use. Document the reason when a new durable state or abstraction is necessary.

## What changes are accepted

From 3.1.2 this plugin is in maintenance. It was meant to be a simple memory plugin, and it stays one by this being the whole list:

- a bug, with a reproduction: a failing test, or numbers read from a running install;
- a security fix;
- a change a host made that the plugin has to follow (Hermes, Codex).

Anything else, a new capability, a new setting, a rewrite for its own sake, needs the owner's decision in words before any code. "Look for what else could be improved" is not a task here: an agent given it says so and stops, and a list of possible improvements is not a reason to make one. Measure before proposing; most of what looks worth doing dissolves once it is measured.

## Release and deployment

- The canonical repository is `github.com/410979729/scope-recall-hermes`; deployable wheels are built only from `main`, at a tagged commit (`v<major>.<minor>.<patch>`), one wheel and one sha256 per tag; the first commit after a tag moves `_version.py` past it. Never `git init` a second history or copy the tree; use `git worktree add` and remove the worktree when the task is merged.
- Production upgrades stop all target gateway/MCP/worker writers and automatic restarters FIRST (planned-stop where supported), then use `maintenance.cli package-upgrade` with an offline wheel and external backup; uv supports pip-less venvs. Follow `maintenance/AGENT_WORKFLOW.md` for activation/recovery. Never hot-edit `site-packages` or remove `~*` remnants as an upgrade procedure.
- After every upgrade run `maintenance.cli plan-install` / `apply-install` so the receipt and host wrapper carry the installed version, then `doctor`; receipt, `pip show` and `running-code` records disagreeing on the version is a defect.
- A temporary setting on a running install (a drain value, a pause) is undone in the same piece of work that made it. One that has to outlive the work is written where the next rollout will trip over it, with the original value and the condition for undoing it.
- Operator CLIs run as `python -I -X utf8 -m scope_recall.maintenance.cli ...` from outside the source tree, so the installed package and not a checkout answers.
- One writer at a time on the release branch; other contributors deliver branches or patches. Run the tiers that own the changed files before committing, `unit` + `contract` + `packaging` before merging, `integration` + `native` before tagging.
- Every new `.py` is either in `packaging/v11-module-allowlist.json` or in `tests/` and selected by a tier in `scripts/check.py`. Files that are neither do not enter the tree.
