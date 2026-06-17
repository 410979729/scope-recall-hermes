# Scope Recall 1.4.0 release-readiness notes

Date: 2026-06-17

## Scope

Version `1.4.0` adds the conservative Experience Kernel MVP on top of the stable V1 Scope Recall contract:

- procedural playbook schema/tables and FTS surfaces;
- `procedural_playbook.v1` validation with per-step `capability_class` and required evidence;
- scope-filtered playbook search/inspect/preflight/stats tools;
- maintenance-gated playbook create/review tools;
- scoped feedback accounting in `experience_runs`;
- optional runtime preflight packet rendering, disabled by default;
- read-only replay benchmarking through `scripts/experience-replay.py`;
- doctor visibility for Experience Kernel tables and counts.

## Local gate evidence

The following commands were run from the repository root before this note was added:

| Check | Result |
|---|---|
| `python -m pytest -q tests/test_installer.py tests/test_experience_schema.py tests/test_experience_tools.py` | `28 passed` |
| metadata check through `scripts/check.release.py` internals | `ok: true`, no missing source, no failures |
| `python -m ruff check .` | `All checks passed!` |
| `git diff --check` | exit code `0`, no whitespace errors |
| `python -m pytest -q` | `327 passed, 2 warnings` |
| `python scripts/check.release.py` | `ok: true` |

Release-gate artifact smoke from `python scripts/check.release.py`:

| Surface | Result |
|---|---|
| Wheel name | `hermes_scope_recall-1.4.0-py3-none-any.whl` |
| Wheel file count | `73` |
| Import smoke | `['register']` |
| Installer smoke | installed plugin manifest version `1.4.0` |
| Doctor smoke on installed wheel copy | `ok: true`, plugin version `1.4.0`, pyproject version `1.4.0` |
| Source scan | no generated artifacts, obvious literal secrets, or private paths |

Runtime doctor was also run against the active Hermes profile with `$HERMES_HOME` set explicitly. It reported:

| Runtime surface | Result |
|---|---|
| Overall doctor | `ok: true` |
| Source metadata | plugin version `1.4.0`, pyproject version `1.4.0`, README public version `1.4.0` |
| SQLite truth | `ok: true` |
| Vector companion | `ok: true`, search smoke `ok` |
| Journal provenance | `ok: true` |
| Experience Kernel | `ok: true`, tables ready |
| Active-profile playbook/run counts | `0` playbooks, `0` runs |

## Experience Kernel canary evidence

A separate sibling-profile canary was run before this release-readiness note. It created one reviewed/promoted playbook and recorded one successful feedback run:

| Canary surface | Result |
|---|---|
| Candidate create | succeeded only under maintenance/operator mode |
| Pre-promotion preflight | `no_reuse` because no promoted playbook existed |
| Review/promote | succeeded; playbook reached `promoted` |
| Post-promotion preflight | `guided_reuse`, non-empty packet |
| Feedback | `success` recorded in `experience_runs` |
| Maintenance disabled afterwards | create/review tools hidden or fail closed |

This proves the minimal reusable-experience loop exists, but it does **not** prove mature large-scale automatic experience quality.

## Release boundaries

- Source-tree gates and wheel/install smoke prove the package candidate is locally coherent.
- They do not prove that a long-running Hermes gateway has already loaded this exact code; runtime freshness requires restart/reload approval and a post-restart smoke on the target service.
- Runtime Experience packet injection remains disabled by default through `experience.prefetch_enabled=false`.
- Playbook create/review remains maintenance-gated; ordinary runtime exposure is limited to read-only search/inspect/preflight/stats plus scoped feedback when `experience.enabled=true`.
- Claims should stay conservative: framework-ready and minimal-loop-proven, not mature automated experience curation.

## Pre-push checklist

Before pushing a branch, PR, or tag:

- [ ] Re-run `python -m ruff check .`.
- [ ] Re-run `git diff --check`.
- [ ] Re-run `python -m pytest -q`.
- [ ] Re-run `python scripts/check.release.py` after this document is present.
- [ ] Review `git diff --stat` and `git diff --name-status`.
- [ ] Confirm the commit/tag target and push scope with the maintainer.
