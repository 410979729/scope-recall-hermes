# Repository Census Summary

This is the redacted, repository-safe summary for the Scope Recall Program 6A
review. The complete per-file inventory is generated locally at
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

## Program 6A disposition

- Public comparison base: `f63c953d8c4ca3c8185671ff0a3cb67579c30b60`
  (tree `6917f5c15ef67815ef6166e9043d0d556af8b510`).
- Exact candidate commit, tree, tracked-file count, category totals, lifecycle
  totals, and inventory hash are generated after the final candidate commit in
  `.execution/evidence/<candidate-sha>/REPOSITORY_CENSUS.json`.
- Those self-referential candidate identity values are deliberately not
  hard-coded in this tracked file: changing this file would create a different
  commit and tree. The final evidence index and candidate manifest bind them.
- No file is deleted or renamed relative to the frozen public base. Intermediate
  construction-branch filenames are not claimed as public-base renames.
- No repository file met the independent-evidence threshold for deletion, so
  no deletion is authorized or planned.
- Executable full-scope relation rebuild behavior is retired in place; retained
  schema and reporting surfaces are registered as compatibility debt.
- Historical release-readiness documents remain historical evidence and do not
  override the current 1.10.6 release contract.
- The committed anomaly register contains no blocking anomaly.
- The final raw census must report a clean worktree, zero untracked files, and
  exact agreement with the source identity in `CANDIDATE_MANIFEST.json`.

Related governance evidence:

- `docs/repository-census.anomalies.json`
- `docs/repository-deletion-evidence.json`
- `docs/compatibility-removal-registry.json`
