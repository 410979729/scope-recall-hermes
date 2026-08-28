# Release-candidate evidence

Scope Recall release candidates are built by one source-bound command. The
command is an audit and handoff tool; it does not merge, tag, publish, release,
or deploy anything.

Run it only from the exact clean candidate commit:

```text
python scripts/build.release_candidate.py --expected-sha <full-40-character-sha>
```

The command performs these operations in one process:

1. Refuses a dirty worktree or a HEAD different from `--expected-sha`.
2. Computes the tracked-source manifest and Git tree identity.
3. Builds one wheel and one sdist in an isolated temporary directory.
4. Scans the real archive members for forbidden private, state, secret, cache,
   and non-allowlisted test paths.
5. Hash-binds wheel runtime Python files and sdist source members to the
   tracked-source manifest.
6. Installs and verifies the wheel and sdist in separate new virtual
   environments with isolated Hermes homes.
7. Runs each allowlisted sdist journal-restore test module as a separate,
   bounded process so a stalled module is identifiable.
8. Writes `BUILD_PROVENANCE.json`, then generates
   `CANDIDATE_MANIFEST.json` from that provenance.

Output is written under the ignored directory:

```text
.execution/evidence/<full-candidate-sha>/
```

`scripts/report.candidate_manifest.py` accepts `--provenance`; it does not
accept arbitrary artifact paths. It recomputes the current commit, tree, source
manifest, artifact hashes, and archive-member hashes. Any mismatch fails with
`CANDIDATE_ARTIFACT_PROVENANCE_MISMATCH`.

The public manifest and provenance contain hashes, counts, version identity,
and normalized commands only. Raw local logs remain in the ignored evidence
directory and are not release artifacts.
