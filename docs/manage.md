# Manage local memories

`scope-recall memory` is the local store owner's interface. The operating system account that can access the
installation can use it. It does not call a model or a network service. Agent tools keep their existing scope and
conversation-authorization checks; they do not expose this installation capability.

Select a shared store with `--root /path/to/store`. For an installation with its own store, use
`--host hermes|codex|claude-code|workbuddy|dsh --instance-root /path/to/home`. An attached entry directs you to the shared
store root so management sees the whole installation. Examples below use a shared root.

## Browse and inspect

```sh
scope-recall memory list --root /path/to/store
scope-recall memory search "release checklist" --root /path/to/store --kind claim
scope-recall memory show claim-REF --root /path/to/store
scope-recall memory history claim-REF --root /path/to/store --limit 20
```

Replace the object reference with one returned by `list`. Kinds are `event` (original source), `claim` (derived fact),
`episode`, `artifact` and `reference`. List and search accept `--kind`, `--scope`, `--agent`, `--session`, `--since` and
`--until`. The agent filter matches an entry ID, its display name or host; a store of its own also accepts its bound
agent ID. Time filters use source occurrence time, or recording time when occurrence time is unknown, and include
both endpoints. Supply an ISO 8601 timestamp with a timezone, for example `2026-01-01T00:00:00Z`.

Search is a literal substring match, case-insensitive for the rendered text. It sends nothing to an embedding
provider. Each call continues until it finds `--limit` matches, examines the remaining inventory or reaches a
30-second time budget. The budget bounds scanning after the inventory is ordered and is checked between hydration
batches. Inventory ordering includes first-revision lookups, timestamp normalization and sorting; it runs before
the first budget check, repeats for every continuation and takes longer as the number of objects grows. Each call
completes at least one small hydration batch before a budget stop, even if ordering has used the budget.
`--limit` accepts 1 to 100 items. List uses the same scan and order: newest first, by an event's first stored readable
revision's occurrence time (recording time when unknown), or by the recording time of another object's first revision.
Object references break timestamp ties. Later revisions keep their position.

Tables and JSON report how many object identities this call examined, whether the scan is complete, and whether it
stopped at the match limit or time budget. An empty result with more remaining says so and displays a continuation
token. JSON exposes `examined`, `scan_complete`, `stop_reason` and `next_cursor`. The count
includes identities rejected by source filters or visibility checks; it is not a count of text matches. Supply the
exact next cursor with `--cursor` and the same command, target and filters. The opaque token uses URL-safe characters
and needs no shell quoting. Continue until the cursor is absent to exhaust a search.

Listings cover every project and branch in the owner's scopes. Each call reads one SQLite snapshot. Captures during
a call or between pages do not invalidate the cursor. Objects
stored after a listing began may be missing from later pages; start again without a cursor to include them. Deletions
remove objects between pages. Blocking an event's earliest readable revision can move that event once; any remaining
readable revision stays listable. Read-blocked objects are excluded in each call's snapshot. Muted objects remain
visible to the owner. A cursor for another installation, owner scope or filter set is refused. History uses `--before` with the returned next
revision instead. `show --revision N` displays a particular version.

Default listings are tables. `--json` emits `scope-recall-management/1`, defined by
[`memory_management.schema.json`](../contracts/memory_management.schema.json). Success has `status: "ok"` and `data`;
a refused operation has `status: "error"`, `code` and `field`, and exits with code 2.

## Find stored credentials

Capture masks detected credential values with `[REDACTED_SECRET]` before storage, indexing or splitting a long
message. Text outside a masked span stays as it was. What counts as a credential is decided by the same scanner
that withholds recall results and checks model requests. An unquoted labelled value is masked through the end of
its line or its structure item (`,` `;` `)` `]` `}`), including any prose after it. A quoted value is masked up
to its closing quote, which stays, when the rest of the line starts with a delimiter, or holds no other quote and
starts with a word set off by a space or with a sentence's end (`。`, `、`, or `.`, `!` or `?` before a space and
a word, or before the line's end). Otherwise (another literal, `+` or `.` concatenation, a markup mark, a word
joined to the quote) the mask runs through the line's end, closing quote included. A literal opened with three
quotes is masked across lines from its first quote to its last and reads as one quoted marker. Key-shaped tokens
are masked over their matched spans. Original Unicode characters are covered by the normalized scan's exact map.

`masked_secret_count` in the prepared capture and capture receipt counts merged replacement spans across all
stored text fields of the original envelope, once before segmentation. An attempt prepared afresh reports its
own count; one that reuses an inbox row, and inbox replay, report that row's count. Hook diagnostics report the
count across the hook's captures, and a remote server's hook log shows `masked <n>`; serialized capture results
also include the count.
No diagnostic includes matched text. Masked captures carry `secret_values_masked` in their gaps and a complete
capture becomes partial.

Capture refuses with `plaintext_secret_rejected` when identity or reference fields contain a secret, including
event keys and session or scope identifiers; those fields are never rewritten. It also refuses when a text field
would become only the marker, the masked result scans as a secret, a private-key block is detected, a literal
opened with three quotes has no closing three, or an inexact character map or a value that may continue on the
next line requires withholding the whole text. Corrections refuse secret values so the caller can rephrase them.

```sh
scope-recall memory secrets --root /path/to/store
scope-recall memory secrets --root /path/to/store --json
```

This read-only command scans readable stored versions, including historical and muted objects, across the owner's
scopes. It lists `ref`, `kind`, `revision`, `origin`, `entry`, `session`, `time` and the names of matched patterns.
Chinese credential labels, camelCase names and Markdown English labels share one value rule: after trailing
punctuation is removed, a printable ASCII value needs at least four characters with a digit or an inner symbol,
or, after a password label (password, passwd, 密码, 口令, 授权码), at least eight letters. A value may begin with
one of `!@#$%^&*+=~?`; other punctuation right after a label belongs to the surrounding text (a list, a link, a
path, JSON). Dotted references, names with underscores and no digits, and camelCase or PascalCase names without
digits are excluded. Chinese text after a Chinese label, or after 是 or 为 behind any label but a password label,
is prose; Chinese-character passwords after Chinese labels pass this scan. Quoted values end at their closing
quote, respecting escapes.
Neither output format includes content or matched text. `rows_read` counts all versions read in SQL windows;
`candidates_scanned` counts those selected by the SQL credential-word prefilter for the full scan. Each batch ends
its read transaction before scanning content. The command uses no model or embedding provider and changes nothing.
Captures and revisions committed behind a batch's cursor require another run.

After upgrading, run this command, review the references and use `scope-recall memory forget <refs>` to remove the
findings through the normal preview and confirmation. Change any credential found, because it was already stored
and may have been sent to a model provider. Agent recall and tools withhold secret-bearing items with a
`secrets_withheld:N` gap. Profile and entity views report partial coverage when they withhold content.
The owner's local page and memory commands keep originals visible for review and deletion.

## Correct a derived fact

```sh
scope-recall memory revise claim-REF --root /path/to/store --expected-revision 3 --value "green"
scope-recall memory revise claim-REF --root /path/to/store --expected-revision 4 --retract
```

The expected revision is mandatory. A stale revision is refused without writing anything. Correction changes the
literal value and starts the new version at the correction time; the claim's subject, predicate and conditions
remain its identity. Earlier versions and original conversation text remain available in history. Original source
text cannot be edited through this interface.

A correction has a host-generated source labeled `local_management`, plus a separate audit action whose actor is
`local_user_management`. It is not recorded as a chat message from a person. The correction's text stays in ordinary
source storage, governed by the same deletion and visibility rules; the audit carries object identities and versions.

## Mute, unmute and delete

```sh
scope-recall memory mute claim-REF --root /path/to/store
scope-recall memory unmute claim-REF --root /path/to/store
scope-recall memory forget claim-REF --root /path/to/store
scope-recall memory forget-session --root /path/to/store --session SESSION-ID
scope-recall memory forget-session --root /path/to/store --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z
```

These commands first display a dependency preview: the selected versions, affected objects, source records and
entries. Deleting a fact also deletes its source group and dependent facts, episodes and attachments, because leaving
the source readable could return the same content on the next recall. This is whole-source deletion, not sentence
redaction. Check the complete preview before confirming. Narrow selections larger than 2,000 affected objects.
Session selection and its preview share one read snapshot. Selection is never cut short by a time budget: it must
finish so a deletion cannot silently leave matching sources behind. A capture during selection makes application
refuse the stale preview; repeat the command to include that capture.

Repeat the command with `--yes` to apply. Include `--preview-id VALUE` to require the exact earlier preview; a change
to the store then causes a refusal. With `--json`, an applied response includes both the preview and the receipt.
Multiple references can be selected together; `--expected-revision N` additionally binds a single selected reference.

Mute stops automatic recall while keeping explicit inspection available. Unmute releases the displayed suppression
closure; it never restores deleted content. Delete immediately blocks reads and enqueues the existing physical purge.
Its receipt describes each layer. `read_blocked: true` alone does not mean physical cleanup finished; consult the
deletion operation until its active-content and history layers report completion. Host transcripts and old backup
files are outside that active-content receipt.

## Migration and rollback

The first open of a supported older store migrates it transactionally to schema 1112. The added tables contain local
management action identities, actor, action, target versions, timestamp and memory epoch, content-free suppression
constraints for records that can arrive after a restore, and private intervals that hold only target, session or
project path, mode and UTC boundaries. Original source rows are preserved; the new `project_root` column is metadata
and existing rows keep NULL. Earlier executables refuse this newer schema; see
[private-mode.md](private-mode.md#migration-and-rollback) for the rollback consequence.

Before installing the new executable, stop the installation's writers and take a `scope-recall backup` SQLite
snapshot; keep the old executable and configuration alongside it. To roll back, use the existing
[`restore` procedure](install.md#going-back-to-a-backup) with that snapshot and a separate backup directory. The newer executable
first records the latest deletion ledger and closes admission; the executable matching the snapshot then completes
the restore. Do not overwrite the database file by hand: that can resurrect deleted memories. Corrections made after
the snapshot are outside its recovery point. A snapshot taken while content was muted can restore that older mute
when completed by an older executable; use the management-capable executable to preserve later unmute decisions.

For management-capable snapshots, restore uses a complete suppression checkpoint, including records that inherited
a mute. For a snapshot predating management, it prepares the original deletion-ledger format that the older
executable understands. Both paths preserve deletions; only the complete checkpoint can release an older mute.
