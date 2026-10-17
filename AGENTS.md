# Scope Recall maintenance rules

Maintainability is a product requirement. Do not let the code bloat: size is not the limit, and a hundred thousand lines and more is fine, but code that is hard to maintain, with weak boundaries or tangled coupling, is not. Readability and maintainability are held to the standard of the world's best engineers. Prefer a small, explicit change to a new framework.

- SQLite is the sole authority for facts, identity, permissions, lifecycle and evidence. Derived indexes are optional and rebuildable.
- Put behavior in one owning module. Host adapters translate requests and bind trusted identity; they do not implement another copy of Core policy. No new import cycles: changing one feature does not drag a string of unrelated files along.
- Extend or compose an existing implementation before adding another engine. Remove superseded production paths in the same change; preserve historical fixtures outside the shipping allowlist. Delete before adding: dead code, duplicate implementations and settings nobody needs go first.
- `scripts/quality.baseline.json` only goes down: no file gains ruff or pyright findings, and no function grows past its recorded size.
- Keep migration orchestration separate from legacy conversion, host activation and background indexing. A host integration must explicitly prove its writer/activation boundary.
- Multi-hop queries are read-only evidence joins. They must not infer identity, broaden audiences, resurrect invalid facts or promote query-time conclusions.
- Long work needs bounded stages, durable progress, idempotent retry and visible failure. Never turn unknown data into a successful migration or retry forever.
- A new public capability needs a shared contract, both applicable host surfaces, concise operator documentation and focused boundary tests. Avoid full-suite repetition without a specific reason.
- Do not add a dependency or an extensibility layer solely for hypothetical future use. A new dependency, durable state or abstraction gives its reason in the pull request.
- Comments and docstrings say what the code does, why, and the constraints a reader would not see; they are written for the next maintainer and never narrate how the code changed. Dates, version numbers, instance names, incident, review and release-candidate numbers, issue numbers and notes on where an idea came from belong in the commit message and the changelog, not in code or docs. Tests are named for what they check, not for the change that added them.
- Code and docs state verified behaviour only: no to-do or to-be-decided markers and no hedging (`scripts/quality.py` lists the markers it refuses). What is not verified is verified or removed, and tracked as an issue. Plans, progress notes, handoffs and research reports stay out of the repository. Comments and docs follow the language of their file.
- A name another module uses is public (no leading underscore). Imports go at the top of the module. An import stays inside a function only in four cases: moving it up would load more when an entry starts, it needs an optional dependency, tests replace what it imports at call time, or it is one of the lazy upward imports `scripts/quality.py` names.

## What changes are accepted

A change answers three questions before any code is written: whose problem does it solve; what shows that the problem exists (a reproduction, a failing test, numbers read from a running install, a user's report); and what will measurably change once it is done. A change that cannot answer all three is not made. A bug, a security fix and a change a host made that the plugin has to follow (Hermes, Codex) answer them by their nature, given a reproduction.

New capabilities are welcome on the same terms, ideas from other memory systems included, when they fit what Scope Recall is: SQLite is the only source of truth, every memory can be traced to its source, several agents share one store and are kept apart by scope, and it all runs locally. Borrow ideas, never code.

Work stops when the next step would be one of these: a feature that duplicates another or solves no problem anyone has; polishing what no one can say is wrong, or whose measured gain is too small to be worth its cost; hunting for bugs that are not there; work found only to keep busy. Measure before proposing; most of what looks worth doing dissolves once it is measured.

When a new path changes an old one, decide on the merits and give the reason in the pull request. A new implementation that covers every use of the old one replaces it, and the old one goes in the same change; two that each do something better become one that keeps both strengths, never two side by side. Stored memories are never lost: a change of stored format comes with an automatic migration, a migration test and a way back. A change that makes another agent or a user edit a configuration or a call by hand says how, in the pull request and in the changelog.

## Working in this repository

- Never run a recursive or bulk delete on a path that is computed or empty, a drive root, a home, or anything outside your own worktree. Delete only what you created, by its exact path, after listing it, and remove a worktree with `git worktree remove`. `git clean -fdx`, `git reset --hard` and `git checkout -- .` never run in a checkout someone else may be using.
- Tests use synthetic data only; a real memory store or a running installation is never test data.

## Release and deployment

- The canonical repository is `github.com/410979729/scope-recall-hermes`; deployable wheels are built only from `main`, at a tagged commit (`v<major>.<minor>.<patch>`), one wheel and one sha256 per tag; the first commit after a tag moves `_version.py` past it. Never `git init` a second history or copy the tree; use `git worktree add` and remove the worktree when the task is merged.
- Work that belongs together ships as one release once all of it is done and verified. Its steps are tested and audited on their branch, then merged and tagged once, not released one by one. A fix a user is waiting for goes out as one pull request with the fix, the version and its notes, and is tagged directly without a release candidate.
- The changelog gives each release a heading and one paragraph, plus what an operator must do beyond installing the package. The release workflow publishes that section as the GitHub release's notes.
- Production upgrades stop all target gateway/MCP/worker writers and automatic restarters FIRST (planned-stop where supported), then use `maintenance.cli package-upgrade` with an offline wheel and external backup; uv supports pip-less venvs. Follow `maintenance/AGENT_WORKFLOW.md` for activation/recovery. Never hot-edit `site-packages` or remove `~*` remnants as an upgrade procedure.
- After every upgrade run `maintenance.cli plan-install` / `apply-install` so the receipt and host wrapper carry the installed version, then `doctor`; receipt, `pip show` and `running-code` records disagreeing on the version is a defect.
- A temporary setting on a running install (a drain value, a pause) is undone in the same piece of work that made it. One that has to outlive the work is written where the next rollout will trip over it, with the original value and the condition for undoing it.
- Operator CLIs run as `python -I -X utf8 -m scope_recall.maintenance.cli ...` from outside the source tree, so the installed package and not a checkout answers.
- A commit carries its human author's identity only: no `Co-authored-by`, `Generated with` or similar line names an AI tool or its account, and neither does a pull request or a comment. GitHub lists such an account among the repository's contributors. `main` was once rewritten to take such lines out, with the trees unchanged, and older release tags still point at the commits from before: build only on a freshly fetched `main`, and never push a branch that holds those older commits.
- One writer at a time on the release branch; other contributors deliver branches or patches. Run the tiers that own the changed files before committing, `unit` + `contract` + `packaging` before merging, `integration` + `native` before tagging. `scripts/quality.py` (CI's `lint` job) must pass: formatting, and no more ruff or pyright findings per file and rule, nor bigger functions, than `scripts/quality.baseline.json` records.
- Every new `.py` is either in `packaging/v11-module-allowlist.json` or in `tests/` and selected by a tier in `scripts/check.py`. Files that are neither do not enter the tree.
