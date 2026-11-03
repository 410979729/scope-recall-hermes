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

## Task handoffs

`handoff_write`, `handoff_list`, `handoff_accept` and `handoff_cancel` are available
on the Codex, Claude Code, WorkBuddy and dsh MCP servers and the Hermes provider.
They use the authenticated entry's read and write scopes. Handoffs are agent
reports attached to task episodes; they do not become facts or consolidation input.

To write a report, pass `protocol_version: "1.1"` and a `report` object:

```json
{
  "task_ref": "episode-reference",
  "progress": "Layout is ready",
  "changed_files": ["src/layout.py"],
  "uncommitted_files": ["src/layout.py"],
  "next_step": "Check the narrow viewport",
  "owner_decisions": [],
  "evidence_refs": ["source-reference@1"],
  "session_label": "sender-conversation-label",
  "ttl_days": 7
}
```

Use a visible task episode and current source references belonging to it. File
paths and progress are reports; the server does not read those files or verify
Git status. `session_label` is optional and always labelled `unverified`; it never
authorizes a write or identifies a unique handoff. The separate `sender_session` comes only from host-bound context, such as Hermes identity, hook or remote ingress context, captured session-end metadata, or reserved MCP thread metadata. Without that attestation it is null, and report-body privacy uses project and store rules rather than the display label. A store-generated reference
identifies each report. A trusted project id, or a host-supplied normalized working
directory when project ids are absent, confines delivery to the same project.
An unverified or missing project directory cannot authorize a directory-based report.

`ttl_days` accepts integers from 1 to 365 and defaults to 7. `handoff_list` returns
at most 30 records by default, up to 100 with `limit`; continue with `next_before`
as `before`. Set `include_closed: true` to include accepted, cancelled and expired
records. Every record includes its confirmation and safety state. Explicit reads
label held reports; they never release a held source's text.

Pass `ref` and `expected_revision` to accept or cancel. Only one entry can accept;
a second attempt reports `already_accepted` and the receiving entry. A stale version
is refused. Cancellation belongs to the sending entry or the local installation
owner through the management page. The owner page uses the existing preview and
confirmation operation. Reports keep their lifecycle audit in SQLite.

At session end, existing task resumes produce unconfirmed drafts. A resume
published after its session end also produces a draft. The sending entry confirms
one through `handoff_write` with `report: {"draft_ref": "handoff-reference",
"expected_revision": 1}`. Confirmation publishes the exact stored snapshot;
changed evidence requires a new report. No additional model call is made.

The session briefing includes at most three confirmed pending handoffs within
`session_brief_tokens`. Each appears as escaped JSON labelled as an external
agent report. Accepted, cancelled, expired, quarantined and pending-review reports
are excluded. Lifecycle, expiry, source visibility and injection policy are checked
again before delivery. Reads apply expiry immediately; the existing briefing
worker records expiry transitions in bounded writes. The final single-injection and session token caps include handoff text. Handoffs do not create correction-notice targets; withdrawn evidence instead prevents the report from being delivered. Inline private spans in report text are removed by the shared capture filter before storage. Turning off
`session_brief_enabled` also turns off automatic handoff delivery. No host memory
file needs to be edited.

The automatic schema upgrade preserves existing facts and keeps the upgrade
coordinator's rollback snapshot. To return to an older release, restore its
pre-upgrade snapshot using the documented restore workflow; older code does not
write the newer schema. Handoffs are local coordination records and are not
included in the portable memory interchange format.
