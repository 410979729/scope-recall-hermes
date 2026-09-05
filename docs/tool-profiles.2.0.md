# Scope Recall 2.0 Tool Profiles

Scope Recall 2.0 uses five named schema profiles. Profiles decide which tool
schemas are offered to the Primary Agent; they do not grant runtime authority.
Maintenance, secrets, temporal queries, reflection, and Experience retain their
independent local feature gates and handler checks.

| Profile | Intended use | Measured tools | Schema chars | Estimated tokens |
|---|---|---:|---:|---:|
| `core` | Default Primary Agent store, recall, context, and compact dispatch | 6 | 9,588 | 2,397 |
| `compatibility` | Historical individual V1 schemas | 20 | 15,830 | 3,958 |
| `maintenance` | Core plus explicitly authorized maintenance | 17 | 17,403 | 4,351 |
| `developer` | Core plus read-only inspection and diagnostics | 13 | 13,058 | 3,265 |
| `extension` | Core plus separately enabled extension tools | 11 | 11,960 | 2,990 |

The measurements use compact, sorted JSON from the exact source schemas in this
candidate and the conservative four-characters-per-token estimate. Maintenance
was measured with `maintenance_tools_enabled=true`; extension was measured with
Experience enabled. The online candidate-review amendment adds 57 core schema
characters over the 2.0.1 baseline after removing redundant parameter descriptions.

The release gate freezes the current core snapshot under Decision D-013: 6
tools, 9,588 schema characters, at most 9,600 characters, at most 2,400
conservatively estimated tokens, and canonical schema SHA-256
`d19b08d445c17c265ee216acfe06060714ac1848917d5e7d70aa0dc05edd615d`.
The count is a reviewed snapshot, not a permanent product-wide tool-count rule.
Any schema or digest change requires an explicit policy/Decision Log update;
moving a tool to another profile remains preferable to silent core growth.

### D-013 amendment: online candidate review (2026-09-05)

Issue #68 requires the live writer to review one candidate without stopping the
gateway. Add `promote`/`archive`, dry-run, and revision parameters to the existing
memory dispatcher. Preserve the six-tool core surface and both existing cost
ceilings. Consolidate redundant descriptions while retaining plan/apply guidance;
update the exact digest and regression snapshot alongside this decision.

`compact` remains an accepted alias for `core`. `standard`, `legacy`, and
`compat` remain accepted aliases for `compatibility`. No public tool name or
dispatcher alias is removed during 2.0.x.

`scope_recall_stats` exposes session-local content-free governance observations:
call, success, error, and alias counts; last-used package version/time; and
whether the tool depends on maintenance authority. It also reports the active
profile's schema budget. It never records arguments, prompts, result bodies, or
private content. Counters reset with the provider process and are operational
observations, not durable memory truth.
