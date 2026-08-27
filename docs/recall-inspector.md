# Recall Inspector

`scope_recall_inspector` is a developer-profile, read-only view of the exact
Recall Packet used by the production search path. It does not query private
tables, run a second retrieval, or execute any correction, archive, or purge
operation.

The inspector reports why each result was selected, whether its compiled truth
state is current, historical, or untracked, conflict status, sanitized
provenance, estimated prompt-token cost, confidence inputs, and a bounded
timeline. Memory content is omitted unless `include_content=true`; summaries
remain sanitized at the normal recall egress boundary.

Correction, archive, and purge entries are plans only. A purge plan is itself
zero-write, requires maintenance tools, and does not bypass the separate deny
and erase confirmations.

Enable the schema with `tool_schema_profile: developer`. The default `core`
profile and the compatibility profile do not expose this diagnostic tool.
