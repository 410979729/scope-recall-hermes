# Scope Recall 2.0 Extension Boundaries

Graph relations, Experience, Playbook, Reflection, and External Bridge are
optional capabilities around SQLite-owned memory truth. None is a current-truth
authority or a Core startup prerequisite.

| Extension | Disable path | Scheduler |
|---|---|---|
| Graph relations | `relation_extraction_enabled=false` and `retrieval.relation_rerank_enabled=false` | Existing Core background owner only |
| Experience | `experience.enabled=false` | Existing Core background owner only |
| Playbook | `experience.enabled=false` | Existing Core background owner only |
| Reflection | `reflection.enabled=false` | None; explicit tool call only |
| External Bridge | No automatic registration or invocation | None; explicit standalone API only |

When Experience is disabled, Core startup does not import its storage,
preflight, promotion, or Playbook implementation and skips Experience-specific
startup backfill. Enabling Experience loads those modules lazily at the admitted
call or shared-background follow-up boundary. Reflection is already lazy-loaded
behind its tool gate. External Bridge is never started by the provider.

`doctor_extensions.extension_report()` and the main Doctor `extensions` check
report every boundary, its enable state, disable path, scheduler owner, and
authority invariant without reading or emitting private content.
