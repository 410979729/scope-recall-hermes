# Private mode

Private mode is the local store owner's control over what Scope Recall may keep,
recall or send. It is a command-line capability. Agent tools do not expose it, and
a model writing the same words is not the owner's command. No slash command or
host-menu entry is added: the supported hosts do not expose a verified surface
that can tell an operator-typed command from model-generated text.

```sh
scope-recall private on --session SESSION-ID --mode full
scope-recall private on --project C:\Work\Project --mode read-only
scope-recall private on --all --for 2h
scope-recall private status --session SESSION-ID
scope-recall private list
scope-recall private off --session SESSION-ID
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
- `--all`: the whole store.

`--mode full` (the default) refuses capture, recall and background model work for
the target. `--mode read-only` refuses capture while recall continues. `--for`
takes a duration such as `30m`, `12h` or `7d`; without it a rule stays until
`off`. A global rule can use a duration and then recovers by itself.

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

This is why the interval table keeps closed intervals: they are the only record
needed to refuse late copies. It stores no message, prompt, attachment body,
question or model answer; its columns are the target, session or normalized
project path, mode, UTC start/end and creation time. A `read-only` interval
refuses events whose own time is at or after its start; a transcript line said
before the rule began may still be captured, while nothing said after it begins
is.

A full interval also refuses a direct capture while it is open, even if the
event itself predates it. Hermes does not keep a failed capture's words in its
retry buffer or `kept-captures/` when the rule table cannot be read. A remote
Codex client may keep a hook in its local spool while the server cannot answer
the metadata check, but it does not send it; the flush checks again first.

## Recall and background outbound work

A full rule blocks automatic recall and the explicit `recall`, `profile`,
`entity` and `trace` tools. The recall gate runs before the query embedding
request, so a blocked query reaches no embedding service and no model. A global
full rule also holds the worker's consolidation, candidate-evaluation and
embedding work; local purge and projection rebuild may still finish. A session
or project full rule holds work whose source belongs to that session or project
while other sessions keep working. Work held this way spends no attempt and is
looked at again later.

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
| ordinary | Stored in the local SQLite store. A consolidation of the source may send up to 32 sources and 16,000 UTF-8 bytes to the configured consolidation model. Embedding sends the bounded source text to the configured embedding service. | The captured event keeps the attachment's metadata and ref; retained bytes stay in the local content-addressed directory and are not sent to a model or embedding service. | Recalled locally; a query may be embedded by the configured embedding service. |
| `read-only` | Not stored, not consolidated and not embedded. The prompt is still used for this turn's recall. | Not newly captured; the automatic recall still runs. | Recalled; the query may be sent to the configured embedding service. |
| `full` | Not stored, not sent by Scope Recall. | Not newly captured. | Not recalled; no query embedding, consolidation or candidate evaluation. |

“Configured service” means the routes in the installation's runtime
configuration: `auxiliary.embedding` and `auxiliary.consolidation`, each with its
own model and endpoint. When those routes are absent or their
`external_embedding` / `external_consolidation` approval is false, Scope Recall
makes no such request. The vector store and the SQLite store are local. A host
can still send the prompt to its own model: that is the host's request, outside
Scope Recall's capture and recall paths.

## Host behavior and limits

- Codex, Claude Code, WorkBuddy and dsh local hooks all capture through the same
  client handler and core write, so a full or read-only interval refuses their
  events at the same boundary.
- A remote client asks its entry's server for a metadata-only private check
  before it sends a hook. Only a session id, a project path and event instants
  travel in that request. If the check cannot be answered, the client does not
  send the content; Codex may keep its hook locally, and the spool flush checks
  again before sending. The server checks the same event time on receipt, so a
  spool entry that was already late is still discarded.
- Hermes writes turns, compressed context, steers, tool results, retries and
  kept captures through `CaptureWriter`, which checks before writing its retry
  buffer or `kept-captures/` file. A `full` or `read-only` interval therefore
  refuses the event before those local copies exist. A Hermes project rule uses
  the gateway's `agent_workspace` only when it is an absolute path; a workspace
  label is not treated as a filesystem path.
- A shared-store client's local MCP server uses that MCP server process's
  working directory as its project directory when the host gives no workspace,
  so a project `full` interval also gates its `recall`, `profile`, `entity` and
  `trace`. The directory is metadata; a tool argument can never name one. A
  remote HTTP MCP request carries no project path: its server process's working
  directory is used, while the project interval still gates the client's hook
  recall and capture before that request.
- dsh's native plugin keeps its own local spool of a turn before its Stop. A
  full or read-only rule prevents Scope Recall from storing or sending those
  lines, but the plugin's own spool is outside Scope Recall's store boundary.

## Migration and rollback

Schema 1112 adds `private_intervals` and the `source_events.project_root`
metadata column. The migration runs in the same transaction as the other
schema steps and does not read or rewrite a captured message. Existing sources
keep their words and get no project root; a project rule applies to captures made
after the upgrade. A store at 1111 or any supported earlier schema is brought
forward automatically on the next explicit open.

Earlier executables cannot read schema 1112. To go back, follow the
[restore procedure](install.md#going-back-to-a-backup) with a snapshot and a
separate backup directory; do not overwrite the database by hand. A rollback to
an older schema cannot carry `private_intervals`, because that schema has no
table for them. Treat the rollback as ending every private rule: keep the old
executable's writers stopped while it runs, and reapply the rules with the newer
executable before accepting new conversation. Memories captured while a rule was
active were never stored, so the snapshot has none to restore.
