# Importing memories

`scope-recall memory import` reads an explicitly selected local source. It makes no model or embedding request,
reads no default home directory and does not change the source. Conversation attachments and tool messages are excluded.

```console
scope-recall memory import --root <shared-store> --source chatgpt --from <export.zip> --source-id personal-account --scope <scope-id>
scope-recall memory import --root <shared-store> --source chatgpt --from <export.zip> --source-id personal-account --yes --preview-id <shown-hash> --scope <scope-id>
```

For an installation with its own store, use `--host <host> --instance-root <home>` instead of `--root`.
The preview needs no store access. Writing requires the normal local owner store selection and an exact preview hash.
A changed source, actor, project or time selection requires another review. Omitting the confirmed second command
cancels the import without touching the destination database.

Keep `--source-id` stable for one originating account or memory collection, including when its export moves to another
path. Give different accounts different source IDs. Repeating an approved import retains existing source versions;
changed content under an existing original ID adds a version. Reimporting an older version preserves the newer head.
Equal text in different original occurrences remains separate evidence.

| Source | Explicit input | Stored author |
|---|---|---|
| `claude-code` | A CLAUDE.md file or a directory of Markdown auto-memory files | Assistant; confirmation required |
| `codex` | A memories directory, or one Markdown file | Assistant; confirmation required |
| `hermes` | A memories directory containing MEMORY.md and USER.md, or one of those files | Assistant; confirmation required |
| `chatgpt` | conversations JSON, an export directory or ZIP; split conversations files are supported | Explicit user messages retain verified human origin; assistant messages require confirmation |
| `claude` | conversations or memories JSON, an export directory or ZIP | Human messages retain verified human origin; assistant messages and memory summaries require confirmation |
| `claude-mem` | SQLite database containing observations and optional session summaries | Assistant summaries; confirmation required |
| `mem0` | JSON list, `results`/`memories` list, or schema-defined structured export | Assistant summaries or unknown source; confirmation required |

Malformed message authors and text blocks are refused before the source role is attested. Unknown chat structures
are never repaired into user testimony. ZIP names must be unambiguous relative paths with no duplicate members;
archive errors omit member names and message contents.

The preview lists every occurrence's role, original origin and confirmation requirement, along with totals by original
project and a time range. `--project <original-label>`, `--since <ISO-instant>` and `--until <ISO-instant>` select a subset.
Time bounds include their endpoints and exclude undated records. Dates with no time zone are refused. A Markdown
frontmatter `modified` or `updated_at` supplies its original time; a record without a source time stays undated.
`--project-id <label>` selects the destination project for all selected records, while the preview retains their original project groups and shows each write destination. Use `--project "(unassigned)"` to select records without an original label. Project labels do not authorize a filesystem path. `--project-root <local-directory>` supplies the local project
identity for private-mode matching when it is known. Every confirmed import requires an explicit `--scope` and store selection. The preview hash binds their normalized paths and the private-mode project directory.
Generated memory thread headers preserve each thread's timestamp and original working-directory label. A generated summary's identity follows its thread, including when its filename changes. Conversation titles do not supply project identity. Claude memory exports preserve their project keys; global summaries stay unassigned unless a destination is selected.

All imported occurrences retain `imported` provenance. The importer attests the parsed source role separately from
permission to import. A conversation's human belongs to that source account, without being equated with the local
owner. Existing evidence rules determine whether any claim can become a fact. Memory files and compressed summaries
stay searchable source material, carry `admission_source_only:import_requires_confirmation`, and receive no priority
work reservation. They cannot independently promote a fact. The owner's later independent evidence uses the normal
confirmation path.

Inline `<private>…</private>` spans are removed by the same filter used for ordinary capture, before indexing, queues
or storage. Nested spans are supported; an unclosed opening tag hides the rest of the message. Receipts carry
`private_content_omitted`. Entirely private messages leave no source. Private-mode rules are checked at the original
event time, including historical rules and undated evidence. Credential masking and deletion blocks remain enforced.

The importer checks at most 32 source identities per read snapshot. Only absent identities can skip their individual version read; a concurrent conflict is rechecked against SQLite. Existing histories retain the normal version lookup.
Each occurrence uses the normal capture transaction and releases its writer lease before the next. Derived work uses
the existing bounded admission queues and worker model budgets. A schema upgrade or unavailable writer stops the pass;
repeat the same approved command after it is available. Completed rows are detected as duplicates. Capture import
attestations are never placed in the ordinary host retry inbox.

Doctor's `memory_import` field shows the most recent pass's totals, processed count, outcome and retryable error.
The content-free receipt lives beside the database in `memory-import.json`; an abandoned running pass is reported as
interrupted. Its counts are advisory: SQLite source versions remain authoritative. Import jobs share an import-only
lock; it does not hold the capture writer or block hooks for the duration of a pass.

Hermes's native memory write notifications record the action and writer reported by the host. `assistant_tool` and
`background_review` are assistant sources. Other writers remain unknown-source edits; a reported human editor has an
unresolved human principal. Editing a file does not establish that its contents are that person's original statement.

One occurrence must fit the 2 MiB JSON envelope limit, including metadata and UTF-8 text, before it is split into segments of at most 65,536 characters. Source files are bounded to 256 MiB each. ZIP conversation members share a 256 MiB uncompressed budget and are read
without extraction. Native session histories use their host transcript readers and are outside these source formats.
