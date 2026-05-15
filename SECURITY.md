# Security Policy

## Supported versions

Security fixes are applied to the current `1.x` release line. Older local development snapshots are not supported as public release lines.

## Reporting a vulnerability

Please report security issues privately through GitHub Security Advisories for this repository when available, or by opening a minimal GitHub issue that states you have a security report without publishing exploit details.

Do not include API keys, tokens, passwords, private Telegram chat IDs, local filesystem paths with secrets, or other credentials in public issues.

Useful report details:

- affected version or commit SHA
- installation mode (`$HERMES_HOME/plugins/scope-recall`, editable install, or wheel smoke)
- whether vector retrieval was enabled
- minimal reproduction steps with placeholder credentials only
- expected vs. actual behavior

## Scope

In scope:

- unintended cross-scope memory recall or deletion
- secret leakage in logs, release artifacts, or exported data
- unsafe filesystem writes or path traversal
- SQL/filter injection in provider-owned storage operations
- data loss in provider-owned SQLite truth rows

Out of scope:

- compromise caused by publishing real credentials in local config files
- hosted embedding provider outages
- vulnerabilities in upstream Hermes, LanceDB, SQLite, or PyArrow unless `scope-recall` uses them unsafely
- best-effort redaction gaps in user-provided free text unless they lead to a concrete leak path

## Security posture

`scope-recall` keeps SQLite as the durable truth source and treats LanceDB as rebuildable companion state. If the vector companion is damaged, prefer repair/rebuild from SQLite truth rather than trusting vector-only rows.
