# Private mode

Private mode is the local store owner's control over what Scope Recall may keep,
recall or send. Use either local owner interface: the command line outside the
conversation or the [management page](ui.md). Agent tools do not expose this
control, and a model writing the same words is not the owner's command. There is
no private-mode agent tool, slash command or host-menu control in this package.

```sh
scope-recall private on --session SESSION-ID --mode full
scope-recall private on --project "C:\Work\Project" --mode read-only
scope-recall private on --all --for 2h
scope-recall private status --session SESSION-ID
scope-recall private list
scope-recall private off --session SESSION-ID
scope-recall private off --project "C:\Work\Project"
scope-recall private off --all
```

The same store selection flags as the management commands apply: `--root` for a
shared store, or `--host hermes|codex|claude-code|workbuddy|dsh` with
`--instance-root` for a host's own store. `--json` emits
`scope-recall-private/1`, defined by
[`private_mode.schema.json`](../contracts/private_mode.schema.json). A refused
command exits with code 2.

## What a rule covers

A rule targets exactly one of:

- `--session SESSION-ID`: the exact session id stored in Scope Recall. For a
  shared store that is the entry-prefixed id shown by `scope-recall memory list`;
  the CLI does not guess or strip an entry.
- `--project PATH`: an absolute directory and all of its descendants. A rule for
  `C:\Work\Project` does not cover `C:\Work\Project2`. Matching is by whole path
  components. On Windows, drive-letter case, path case and both separators are
  treated alike, so `C:\Work\Project` and `c:/work/project/src` match.
  Windows extended path spellings (`\\?\C:\Work\Project` and
  `\\?\UNC\server\share\Project`) have the same identity as their ordinary
  forms. Rules resolve directory junctions and symbolic links on the machine
  that owns the path; local hooks bind the actual working directory to that
  same canonical directory. An installation's broader configured root still
  controls authorization, but does not replace the hook's working directory.
  A remote client resolves its own directory before sending its identity; the
  server compares that identity without resolving it against server directories.
  UNC share roots include their children. A directory that does not exist yet
  uses its normalized spelling. If a directory exists but its real location
  cannot be verified, capture and outbound work for it are refused while the
  store has any project-rule history, even after those rules end. Fix access to
  the directory before using it again. Without project rules it behaves normally.
- `--all`: the whole store.

`--mode full` (the default) refuses capture, recall and background model work for
the target. `--mode read-only` refuses capture while agent recall and outbound
work on existing memory continue. `--for` is
accepted only with `--all` and takes a duration such as `30m`,
`12h` or `7d`. Without it a rule stays until `off`. A timed rule ends on time
without another command; `off --all` can end it early.

The owner can still browse, inspect history, correct and manage stored memory
with the local memory command or [management page](ui.md) while private mode
applies under either mode. Owner inspection makes no model or embedding request
and preserves scope and deletion checks. Agents cannot use owner inspection;
full mode blocks their recall and outbound work, while read-only permits both
for existing memory. An agent's explicit forget or correction request still
applies under either mode, with its usual authorization and revision checks.

Overlapping rules take the stricter mode: `full` beats `read-only`. Closing one
rule never closes another, so `off --all` does not release a session or project
rule, and `off --session` does not release the whole store.

`status` shows the effective mode for one session or project (including a global
rule) and its active rules; without a target it shows the global mode and every
active rule. `list` shows active intervals by default and `--all` includes
closed ones. Only interval metadata is returned: target, session or project,
mode, start, end and whether it is active.

## Capture is decided by event time

Every capture path asks the same core policy before it writes. This covers the
client hooks (`UserPromptSubmit`, `Stop`, `PostToolUse`, `SessionEnd`,
`Interrupt`), a Claude Code transcript read, a remote client's spool and its
server's receipt, and Hermes turn sync, compression pre-save, session end,
retries, kept captures and attachment metadata.

The decision uses the event's own occurrence time, not the time the write runs.
If any part of a message occurred while an interval was open, the whole capture
is refused. That refusal survives turning the rule off: a transcript backfill, a
retry, a replay or a late spool entry still sees its old time. Events that
occurred after a closed interval are ordinary capture again.

A callback that observes a new reply can retain that observation's time. An old
message first encountered in conversation history keeps an unknown occurrence
time unless an earlier observation or the host established it. Uploading, writing
or finishing a turn does not give its old messages a new occurrence time.
Without an established occurrence time,
Scope Recall refuses it if any rule for its targets started before it was
recorded, even if that rule has ended. An event with no possible earlier private
interval is captured as usual; stores that never use private mode are unaffected.

This is why the interval table keeps closed intervals: they are the only record
needed to refuse late copies. It stores no message, prompt, attachment body,
question or model answer; its columns are the target, session or normalized
project path, mode, UTC start/end and creation time. A `read-only` interval
refuses events whose own time is at or after its start; a transcript line said
before the rule began may still be captured, while nothing said after it begins
is.

Events from before the interval remain eligible for backfill. Hermes preserves
the first observed time when a later callback repeats an event. A capture whose
private state cannot be read is neither buffered nor kept on disk by Hermes.
Remote clients and dsh remember whether their store has ever had a private rule.
Until they hear that it has, they keep ordinary offline queues without checking
each message first. Once they know a rule exists, a failed check sends nothing
and keeps no new queue entry; an older entry waits for a successful check.

## Recall and background outbound work

A full rule blocks automatic recall and the explicit `recall`, `profile`,
`entity` and `trace` tools. The recall gate runs before the query embedding
request, so a blocked query reaches no embedding service and no model. A global
full rule also holds the worker's consolidation, candidate-evaluation and
embedding work; local purge and projection rebuild may still finish. A session
or project full rule holds work whose source belongs to that session or project
while other sessions keep working. Work held this way spends no attempt and is
looked at again later. Candidate evaluation checks both the candidate's own
source evidence and the evidence selected for the question. A private source
cannot be sent indirectly through a candidate's value or conditions. Startup
embedding warm-up also obeys global full mode.

A full rule invalidates automatic recall and explicit reads, including profile,
entity, trace, object inspection and collection. Each checks again before releasing
content, including when full mode opened and closed during the lookup. Provider
requests carry that conversation's or source's privacy check through queues and
transport locks. Each physical request checks after obtaining its connection or
lock, including each part of a split embedding batch. Remote clients that know
private rules exist also recheck after opening the content connection. There remains a window
between the last check and transmission: content already leaving a remote client
can reach the owner's own entry server. That server still rejects captures whose
event time falls inside the interval. A request already transmitted to a
configured embedding or model service cannot be recalled.

A read-only rule does not block recall. Its question may still be sent to the
configured embedding service, exactly as an ordinary recall's question is. It
does not block consolidation or embedding of memories stored before the rule; use
`full` when that outbound work must stop too.

The hooks that receive an automatic recall inject one line saying that the
conversation is not being remembered, so the model can tell the user plainly.
A full rule injects only that line. The line is not a substitute for the core
gate: it is a user-facing explanation.

## What is stored and what is sent

| Mode | New words and questions | Attachments | Existing memories |
|---|---|---|---|
| ordinary | Stored in the local SQLite store. Source text may be sent to the configured consolidation model. Embedding sends the bounded source text to the configured embedding service. | The captured event keeps the attachment's metadata and ref; retained bytes stay in the local content-addressed directory and are not sent to a model or embedding service. | Recalled locally; a query may be embedded by the configured embedding service. |
| `read-only` | Not stored or sent for capture. The prompt or explicit recall question may be sent to the configured embedding service for recall. | Not newly captured; the automatic recall still runs. | Recalled; the query may be sent to the configured embedding service. |
| `full` | Not stored in the memory store or sent to an embedding or model service. Remote queue and delivery limits are explained below. | Not newly captured. | Not recalled; no query embedding, consolidation or candidate evaluation. |

“Configured service” means the routes in the installation's runtime
configuration: `auxiliary.embedding` and `auxiliary.consolidation`, each with its
own model and endpoint. When those routes are absent or their
`external_embedding` / `external_consolidation` approval is false, Scope Recall
makes no such request. The vector store and the SQLite store are local. A host
can still send the prompt to its own model: that is the host's request, outside
Scope Recall's capture and recall paths. Private mode also leaves the host's own
conversation history under the host's control.

## Host behavior and limits

Each remote hook answer and privacy-check answer tells the client whether its
store holds any private rule, including a rule that has ended. The client saves
only that yes-or-no answer. A client with no saved answer starts with ordinary
queues. Stores that have never used private mode therefore keep their usual
offline behavior and make no extra requests before sending a hook.

After the first private rule is created, the next hook from a client that has
not heard about it can still carry content to the owner's own server. The server
drops private events by the time they happened, before capture or recall. Its
answer makes the client's next hook use strict checks. A message queued on that
client during this window stays there until its next successful check discards
it without sending it. Creating a rule on the server cannot immediately reach
a client that is offline.

- Codex, Claude Code, WorkBuddy and dsh local hooks all capture through the same
  client handler and core write, so a full or read-only interval refuses their
  events at the same boundary.
- A remote client whose saved answer says no sends hooks directly. Codex hooks
  are queued before sending, including while the server is unreachable; the next
  hook that reaches it starts sending the queue. When the saved answer says yes,
  the client asks its entry's server for a content-free check before keeping or
  sending a hook, and checks again before transmission. Only a session id, a
  project path and event times travel in that request. If the check cannot be
  answered, the client sends no content and keeps no new queue entry. Existing
  entries wait for verification; refused entries are discarded without sending.
  Read-only recall sends the question and
  routing metadata to the configured entry server, without attachments or a new
  capture spool. The server checks the same event time on receipt, so a
  spool entry that was already late is still discarded. The protocol records
  the client's send and response-receipt times together with the server's receipt.
  These establish a range of possible times, including network delay. A record is
  refused if any part of that range overlaps an applicable private interval;
  public records near its boundaries can therefore also be refused. Each queued
  observation, including a native dsh spool record, retains its own calibration.
  A new upload's calibration never dates older transcript lines. Without their
  own calibration the server applies the undated-event rule. Each transcript line
  receives its own decision: private lines are dropped locally, their cursor
  positions advance, and eligible later lines are delivered.
  An old question that could belong to a full interval is refused as well; turning
  on read-only mode later does not release it as a recall question.
- Hermes writes turns, compressed context, steers, tool results, retries and
  kept captures through the capture writer, which checks before writing its retry
  buffer or `kept-captures/` file. A `full` or `read-only` interval therefore
  refuses the event before those local copies exist. Deferred assistant replies
  and user steers keep no private body in memory; only identity and observation
  times are retained for delayed callbacks. A discarded retry is removed once
  and is not counted as persisted at shutdown or expiry, including a write that
  finishes after shutdown. Historical messages without an established time are
  checked before their bodies enter those caches. A Hermes project rule uses
  the gateway's `agent_workspace` only when it is an absolute path; a workspace
  label is not treated as a filesystem path.
- A shared-store client's local MCP server uses that MCP server process's
  working directory as its project directory when the host gives no workspace,
  so a project `full` interval also gates its `recall`, `profile`, `entity` and
  `trace`. The directory is metadata; a tool argument can never name one. A
  remote HTTP MCP request carries no project path: its server process's working
  directory is used, so a project rule for the remote directory cannot identify that tool call.
  Use `--all` for remote tools that provide no trusted directory.
- A client that provides no trusted conversation id on its tool boundary cannot
  apply a session rule to that tool call. Use `--all` for those tools.
- dsh's native plugin uses its saved answer for messages and recovery. When it
  says no, messages are appended at once without starting a check process, and
  recovery adopts old queues as usual. When it says yes, each message needs a
  content-free Core decision before it reaches disk. Full, read-only or an
  unverifiable decision drops the new record. Sending and recovery check old
  records using their original time and directory in bounded batches. A refused
  record is discarded without sending or copying it. When a check is unavailable,
  the old file stays available for retry and its plaintext is not copied. The host's own conversation log
  remains under the host's control.

The yes-or-no answer uses an indexed existence check without reading content.
Current-state checks query active rules; capture queries only the indexed
session, global and ancestor-directory intervals that can cover the event.
Closed history remains available for late events without being loaded for every
ordinary hook or recall.

Full backup applies the same capture decisions to upload spools, kept captures
and the database's waiting capture queue. Refused bodies are omitted from the
bundle; the original queues are left alone. The database snapshot and disaster
recovery preserve private-interval history, so late events from those intervals
remain refused after recovery. Backups do not include Hermes's live memory buffer.
When a remote spool cannot be tied to one shared entry, a matching private
interval for any possible entry refuses its copy. Other sessions remain eligible.

## Migration and rollback

The automatic schema migration adds `private_intervals` and the `source_events.project_root`
metadata column. The migration runs in the same transaction as the other
schema steps and does not read or rewrite a captured message. Existing sources
keep their words and get no project root; a project rule applies to captures made
after the upgrade. Supported older stores are brought forward on the next explicit open.

Take a database snapshot before upgrading if you need to go back. Earlier
executables cannot read the upgraded schema. To go back, follow the
[restore procedure](install.md#going-back-to-a-backup) with a snapshot taken before the upgrade and a
separate backup directory; do not overwrite the database by hand. A rollback to
an older schema cannot carry `private_intervals`, because that schema has no
table for them. Keep the older executable's writers stopped: it cannot reject
private transcript backfill or retries. Keep the current-schema backup that
`restore` creates as well as the pre-upgrade snapshot. Before enabling capture
again, open the store with the newer executable and restore that current-schema
backup to recover the complete interval history. Reopening active rules alone
does not protect records from closed intervals. Private conversations are absent
from both snapshots because Scope Recall did not capture them.
