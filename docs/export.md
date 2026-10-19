# Export local memories

`scope-recall memory export` writes a portable package the store owner can keep,
read or move to another tool. It runs as the local store owner, makes no model
request and does not need an agent entry.

```sh
scope-recall memory export --root /path/to/store --out /path/to/TEST-export
scope-recall memory export --root /path/to/store --out /path/to/TEST-export.zip --with-attachments
scope-recall memory export --host codex --instance-root /path/to/home --out /path/to/TEST-export \
  --session SESSION-ID --since 2026-01-01T00:00:00Z
```

The same store selection flags as the other management commands apply: `--root`
for a shared store, or `--host` and `--instance-root` for a host's own store.
`--out` must name a path that does not exist. A path ending in `.zip` creates a
zip archive; every other path creates a directory. An existing target is
refused and never overwritten.

## Filters

`--kind`, `--scope`, `--agent`, `--session`, `--since` and `--until` use the
management filter contract. Time filters compare source occurrence time, or
recording time when occurrence time is unknown, and include both endpoints.
Supply an ISO 8601 timestamp with a timezone.

`--kind` accepts `event`, `claim` or `episode`; without it the package contains
all three. Sources are always written, because they are the original words a
fact or episode rests on. `--kind event` writes only those sources, while
`--kind claim` adds facts and their earlier versions and `--kind episode` adds
episode heads.

A fact, episode or attachment is selected only when **every** source version it
cites is visible and matches the same filters. The selected source set is
therefore a superset of every citation in the package, including the older
source revisions that a historical fact version cites. A record whose evidence
is partly outside the requested range is left out rather than exported with an
unexplainable citation.

Muted records are visible to the owner and are exported with
`"suppressed": true`. Deleted records are not visible and are omitted from every
dataset and from the readable Markdown.

## Package layout

```text
manifest.json
sources.jsonl
facts.jsonl
fact_history.jsonl
episodes.jsonl
readable/
  index.md
  sessions/<session-hash>.md
  months/<YYYY-MM>.md
  facts.md
attachments/                  # only with --with-attachments
```

All JSONL files are UTF-8, one compact JSON object per line. Times are UTC ISO
8601 with a `Z` suffix. The JSON Schema for each record kind is
[`memory_export_source.schema.json`](../contracts/memory_export_source.schema.json),
[`memory_export_fact.schema.json`](../contracts/memory_export_fact.schema.json),
[`memory_export_fact_history.schema.json`](../contracts/memory_export_fact_history.schema.json)
and
[`memory_export_episode.schema.json`](../contracts/memory_export_episode.schema.json);
the manifest is
[`memory_export_manifest.schema.json`](../contracts/memory_export_manifest.schema.json).

`sources.jsonl` holds the original text exactly as stored, with its ref and
revision, scope, project and branch, source agent and entry, session, role,
origin, occurrence and recording times, and content digest. Segmented messages
retain their group key, zero-based index, total and truncation flag; capture gaps
are explicit. The reading views keep a message's segments in their original order.

`facts.jsonl` holds each selected claim's current revision. A record carries the
claim kind, subject, predicate, value, conditions, the claim payload without its
quoted evidence spans, state, basis, qualification reason, validity and recording
intervals, replacement and conflict revisions, suppression flag, and every
source version with its agent, entry, session and times.

`fact_history.jsonl` uses the same record shape for every non-current revision
of the same claims, including late imports and unaccepted candidates with a
higher revision number than the current head. Current revisions are only in
`facts.jsonl`.

`episodes.jsonl` holds episode heads with state, suppression flag, source
watermark, resume state and cited sources.

`readable/sessions/` and `readable/months/` contain verbatim messages under
headings by UTC time, role and origin, each with its source ref, revision, agent
and entry. `readable/facts.md` shows current facts and earlier versions with
their validity, state and source versions. `readable/index.md` links the groups
and gives their message counts. File names use a digest of the full session id
so an id cannot escape the export directory; the heading keeps the original id.

## Manifest

`manifest.json` is written last, after every other artifact. It carries:

- the format `scope-recall-export/1`, the product name and version;
- the UTC export time and the `memory_epoch` of the snapshot;
- the installation scopes and the exact filters used;
- the record counts for `sources`, `facts`, `fact_history`, `episodes` and
  `attachments`;
- the selected attachment metadata;
- one `{path, sha256, bytes}` entry for every artifact except the manifest
  itself.

A package without `manifest.json` is incomplete and must not be treated as an
export. When an export fails, the command removes the files and empty directories
it created, including partial bodies. It refuses output paths containing links
and never overwrites an existing target.

## Attachments

`--with-attachments` includes retained artifact heads whose evidence also
matches the filters. Files are read through the retained-artifact boundary,
which verifies the content-addressed relative path, the installation identity,
the digest, the media type and every path component for links. An arbitrary
filesystem path or directory link is never accepted as an attachment.

## Streaming and snapshot behavior

The datasets are read in bounded pages and written as they are produced; the zip
writer streams each entry instead of building the archive in memory. The export
keeps one read snapshot, so every dataset describes the same store state. Before
writing the manifest it checks again that no deletion or suppression was recorded
in the exported scopes since the snapshot began, then briefly holds the truth
writer transaction until the manifest and archive footer are complete. A
concurrent deletion or suppression therefore refuses publication with
`VERSION_CONFLICT` / `memory_epoch` rather than producing a package that claims
deleted content is current. Record bodies use bounded pages; the manifest and
archive directory retain metadata proportional to the number of output files.

`--json` returns the management envelope `scope-recall-management/1` with the
target path, package format, archive flag and record counts. A refused export
returns `status: "error"`, a code and a field, and exits with code 2.
