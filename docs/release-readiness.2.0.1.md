# Scope Recall 2.0.1 Release Readiness

Date: 2026-08-30

Owner: maintainers.

## Runtime evidence policy

Release evidence must be generated from the exact candidate bytes. Mutable
memory rows, runtime counters, machine-specific paths, credentials, active
configuration values, and deployment overlays are not embedded in this public
readiness note. Deployment remains a later operator gate.

This maintainer note describes the `2.0.1` candidate cumulative since the last
tagged and packaged public release, `2.0.0`.

## Candidate scope

- Preserve SQLite truth, stable V1 provider/tool identities, additive schema,
  and the N-1/N/N-1 rollback window established by the 2.0 product contract.
- Give ordinary users one decision-free update command with no repository,
  URL, candidate, checksum, memory-adjudication, embedding, or repair choice.
- Freeze and verify an official stable candidate outside the replaceable plugin
  tree before stopping the exact target Hermes gateway.
- Persist sealed operation and activation evidence so retry is resumable and
  idempotent after a process crash or reboot.
- Start the candidate only after a proved commit, restart N-1 only after proved
  compensation, and keep writers stopped when evidence is ambiguous.
- Preserve unsafe vector companions as rebuildable debt without deletion or
  memory-content egress; retain SQLite and lexical recall.
- Produce deterministic stable source/manifest Release assets without weakening
  wheel, sdist, provenance, private-overlay, or secret gates.
- H1: require admissible lexical, exact-identifier, calibrated vector, curated,
  or temporal evidence before Search, Context, or Prefetch may return a memory;
  random opaque input must produce an empty result.
- H2: reject transport wrappers before Event Digest candidate persistence,
  preserve review-first candidate isolation, and keep candidates visible only
  through explicit Profile/Review surfaces.
- O1: report Fact adoption enablement, claims, projections, evidence, coverage,
  shadow-backfill state, and last apply evidence without creating another fact
  authority or running backfill automatically.
- O2: distinguish journal, legacy-nightly, and external-Hermes curation status,
  and make the configured curation owner explicit without claiming an
  unobserved external scheduler is healthy.

## Release identity

- Package/plugin version: `2.0.1`.
- Public release baseline: `2.0.0`.
- Expected annotated release tag: `v2.0.1`.
- Published `2.0.0` source, tag, GitHub Release, and PyPI artifacts remain
  immutable.

## Required gates

- Focused managed-upgrade, installer-transaction, stable-download, deterministic
  release-asset, and PyPI asset-separation regressions pass on the exact source
  epoch before the final broader release gate.
- Full pytest, Ruff, Pyright, source diff checks, release invariants, and the
  repository release checker pass on one exact source epoch.
- Windows, Linux, and macOS CI bind to the exact candidate commit.
- Wheel, PyPI sdist, stable USTAR source, and stable manifest are built once from
  the tagged checkout; all four hashes are listed and independently verified.
- Clean install/import/CLI/Doctor smoke and N-1-to-N managed-upgrade rehearsal
  use disposable Hermes homes and never touch an active instance.
- Independent review binds its verdict to the final candidate manifest before
  merge, tag, GitHub Release, PyPI publication, or deployment.

## Current bounded hotfix evidence

- `scripts/benchmark.negative_retrieval.py` currently recomputes 30 negative
  Search/Context/Prefetch surfaces with `negative_nonempty_count=0` and six
  positive evidence lanes, including a Chinese preference query, with
  `positive_hit_rate=1.0`.
- `scripts/rehearse.candidate_isolation.py` currently recomputes zero wrapper
  inserts, zero ordinary candidate leaks, zero unreviewed auto-promotions,
  explicit Profile/Review visibility, and zero read-boundary SQLite changes in
  a disposable database.
- Both current-code outputs are schema-validated and compared exactly with
  frozen public JSON by the release checker. The focused H1/H2/O1/O2 and exact
  `2.0.0` patch-baseline regressions pass on the current source bytes. These
  bounded checks do not replace the final full test suites, multi-platform CI,
  packaging checks, or independent final review.

## Compatibility and rollback

Managed upgrade does not semantically rewrite memory rows. Doctor storage,
configuration, source, extension, pipeline, and temporal safety failures block
activation; quality and rebuildable-companion findings remain visible advisory
debt. Normal failure compensation restores the captured plugin, configuration,
and SQLite epoch only when current-state comparison proves it safe. Unknown
drift never authorizes an automatic overwrite or writer restart.

## Authorization boundary

This document describes a source candidate only. It does not authorize merge,
tag, GitHub Release, PyPI publication, deployment, live migration, live repair,
or changes to an active Hermes instance.

Clearance condition: an independent review must bind an approval verdict to the
exact final candidate manifest and successful required CI before any later
publication or deployment authorization is considered.
