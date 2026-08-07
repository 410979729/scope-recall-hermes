# Scope Recall 1.9.1 Release Readiness

Date: 2026-08-07

This public maintainer note records the release requirements for the cumulative `1.9.1` source tree since the last tagged public release, `1.8.7`. It excludes environment-specific health counters, filesystem locations, credentials, and private validation context. No tag, GitHub Release, or PyPI publication is implied by this document.

## Patch scope

- `1.9.1` carries forward the complete `1.9.0` feature and compatibility scope documented in [`release-readiness.1.9.0.md`](release-readiness.1.9.0.md).
- The patch corrects the lexical-doctor release fixture so it creates its SQLite truth store through the production truth-connection boundary.
- POSIX validation therefore exercises the same owner-only `0700` directory and `0600` database contract as supported runtime creation paths.
- Runtime doctor behavior, lexical migration identity, provider/tool names, SQLite authority, and public APIs are unchanged.
- The stable lexical migration remains associated with plugin version `1.9.0`; the package and plugin distribution version is `1.9.1`.

## Release identity

- Package/plugin version: `1.9.1`.
- Public release baseline: `1.8.7`.
- Expected annotated release tag: `v1.9.1`.
- The untagged `1.9.0` main-branch candidate is superseded and must not be published as a release artifact.

## Runtime evidence policy

Owner: maintainers.

Runtime health is environment-specific and is not embedded in this public source document. Operators may supply explicit dashboard and migration receipts to local release tooling when they need an environment-specific check; those results remain outside tagged package documentation.

## Clearance condition

Clearance condition: every mandatory source, artifact, clean-tree, CI, tagged-identity, and publication check below must pass on the exact release commit.

## Required source and artifact gates

Every item below is mandatory on the exact clean release commit before any tag or public artifact publication:

- Ruff, Pyright, the complete pytest suite, strict release invariants, benchmarks, and source/artifact scans pass.
- Linux Python 3.11 and 3.12, macOS Python 3.12, Windows Python 3.12, Windows installer, and macOS LanceDB smoke jobs pass on the exact commit.
- The lexical-doctor healthy-generation test reports both safe truth-store permissions and a healthy active lexical generation without writing to the database.
- Wheel and sdist contents import successfully and install into a clean environment outside the source tree.
- The release commit, annotated tag, GitHub Release assets, installed metadata, and PyPI artifacts all identify version `1.9.1`.

## Publication boundary

Pushing the source branch does not authorize a tag, GitHub Release, PyPI publication, deployment, plugin reload, or live migration. Those remain separate operator actions.
