# Host capture and identity

Hermes, Codex, Claude Code, WorkBuddy and dsh share immutable installation bindings and exact audience grants.
Session ids distinguish conversations; they do not attest a person's identity. Local Hermes surfaces require the
owner's approved grant. A shared coding-client entry uses the route approved when it was attached. An unknown route
never borrows another route's scopes.

The host declarations in `adapters/host_profiles.py` specify session and turn fields, lifecycle events, application
messages and local surfaces. The shared interpreter removes complete application envelopes before treating visible
words as evidence. Codex rollout recovery reads visible response messages and excludes analysis, tool traffic and
application suggestion requests. Claude Code reminders and task notices, and WorkBuddy wrappers, meta messages and
subagent records retain their host-specific filtering.

## Projects and linked worktrees

A local installation recognizes a linked worktree only when all four checks agree: its `.git` file names an
administrative directory below a registered repository's `.git/worktrees`, `commondir` resolves to that repository's
common directory, the administrative `gitdir` points back to the same worktree's `.git` file, and that common directory
belongs to a registered project root. Nested directories inherit that project's scopes. Invalid pointers remain
unrecognized. No Git process or remote URL grants access. A shared entry continues to use its attached audience.

## Tool inputs

The SDK's internal argument model remains the validation authority. The advertised input schema uses typed JSON Schema
unions for nullable values and enums for literal values, without changing required fields or defaults. Optional
`request_id`, `as_of`, `focus_refs`, `target`, `predicate`, `evidence_refs` and `reason` accept omission and null as their
existing unset meaning. Required arguments still require presence. The required `new_value` accepts any JSON value;
null withdraws the fact. Required `valid_from` accepts null and preserves it in the mutation request. Integer
arguments remain strict integers; booleans and numeric strings cannot substitute for them. Extra identity, scope,
path and session arguments remain forbidden.

## Recovery and privacy

The next end hook and the existing background worker reconcile session ledgers with captured sources. A timed-out or
killed hook does not have to return to make its host's recorded messages recoverable. Checkpoints are disposable;
repeated reads deduplicate against storage. Current grants, private intervals at the original message time and
inline `<private>...</private>` filtering apply on every replay. Recorded working directories and context-only
checkpoints preserve project privacy across resumed sessions. Missing or unverifiable directory provenance uses the
existing unverified-project policy rather than borrowing the host home as a verified project. Remote transport and its spool also strip inline
private words before making a copy. The native dsh spool calls the same local capture filter before writing tagged
text, even for a remote entry; an unavailable filter withholds that message. Ordinary messages need no filter process.
Worker and doctor expose bounded replay progress and recovery actions in
[Background work](background-work.md).

## Stdio lifetime

MCP stdio services observe both input EOF and the launching process's lifetime. On Windows a virtual-environment
redirector is distinguished from its launcher. Disconnect initiates cancellation and allows at most three seconds
for service cleanup. Hermes uses the host's native memory-provider lifecycle rather than a separate MCP stdio process.

## Resident recall

The background worker starts one recall process for the shared library, discovered through a directory private to
this OS user. Every entry uses the same library lock and signed loopback transport. Requests name their entry and
host; the service reloads the attachment before choosing its audience-specific handler. Entries cannot borrow one
another's grants, including after a grant or attachment changes. Capture remains synchronous in the invoking hook.

Hooks and MCP services publish activity but never launch or keep a separate recall process. Worker passes do not
extend idle time. The process exits after the longest enabled entry idle allowance, when all entries disable it,
when its installed package is replaced, or when recall remains stuck. A missing runtime config starts no service;
a transient unreadable config receives a second check before shutdown. The default allowance is 120 minutes for
local coding clients. An explicit `resident_recall_minutes: 0` remains a way to disable residence when no other entry
has it enabled. The worker's explicit zero stops the library service.

Existing hook and MCP launch commands and their installed timeouts remain unchanged. Refresh the native dsh plugin
with the existing `apply-install` workflow before restarting dsh; remote entries use the remote client installation
command. A package-only upgrade leaves that copied JavaScript file unchanged. Doctor checks running package code,
but does not compare this plugin copy against the package. The background worker must be configured and running to
provide resident recall. Without it hooks retain their bounded direct recall and ledger recovery, with any unavailable
or late channel recorded in diagnostics.
