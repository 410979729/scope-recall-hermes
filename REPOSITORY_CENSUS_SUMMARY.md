# Repository Census Summary

This is the redacted, repository-safe summary for the Scope Recall Program 0
baseline. The complete per-file inventory is generated locally at
`.execution/FULL_REPOSITORY_FILE_CENSUS.json` and is never packaged or
committed.

## Boundary and method

- Producer: `scripts/report.repository_census.py`
- Schema: `docs/repository-census.schema.json`
- Path authority: Git tracked files plus non-ignored untracked candidate files
- Content evidence: SHA-256 and byte size only; file contents are not copied
- Excluded boundary: Git-ignored runtime, virtual-environment, cache, database,
  credential, and `.execution` evidence
- Determinism: paths are sorted and the inventory hash is SHA-256 over canonical
  UTF-8 JSON for the complete file-entry array

## Program 0 disposition

- Candidate inventory: 604 repository files.
- Category totals: 215 runtime, 247 test, 57 documentation, 47 operator
  scripts, 7 workflows, 7 benchmarks, 7 governance, 5 metadata, 4 dependency,
  4 example, and 4 other repository files.
- Lifecycle totals: 254 verification, 215 production, 47 operator, 35
  reference, 26 historical, 16 build/release, and 11 maintainer files.
- No case-fold path collision or file larger than 5 MiB was found.
- No repository file deletion is authorized or planned.
- Executable full-scope relation rebuild behavior is retired in place; retained
  schema and reporting surfaces are registered as compatibility debt.
- Historical release-readiness documents remain historical evidence and do not
  override the current 1.10.6 release contract.
- The committed anomaly register contains no blocking anomaly.
- The exact inventory hash, commit, and tree are taken from the final local
  census and G0 baseline manifest after the candidate commit is frozen.

Related governance evidence:

- `docs/repository-census.anomalies.json`
- `docs/repository-deletion-evidence.json`
- `docs/compatibility-removal-registry.json`
