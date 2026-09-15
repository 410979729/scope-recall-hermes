# Source context provenance (3.0.0 / protocol 1.1)

Optional `source_context` on `SourceEvent` is only `{platform, chat_type}`. The Hermes adapter stamps it at the common `_capture_event` boundary from the initialized `HermesIdentity.scope`. Message text, tool arguments, and the recalling session do not supply or replace it. `origin` is unchanged.

Optional `source_contexts` on `RecallItem` (and the recall renderer) is a de-duplicated, bounded list of those pairs. Direct events copy their own context. Visible claim/episode/procedure evidence contributes every distinct pair; mixed evidence is not collapsed to one platform. The field is omitted when metadata is absent. Budget charging uses the rendered payload that includes this field when present.

Old rows without `source_context` stay absent/unknown. `extra_json` already persists unknown optional SourceEvent fields, so no database migration is required. Schema rejects extra identity fields (`chat_id`, `user_id`, …).
