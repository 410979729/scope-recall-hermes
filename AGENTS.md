# Scope Recall maintenance rules

Maintainability is a product requirement. Prefer a small, explicit change to a new framework.

- SQLite is the sole authority for facts, identity, permissions, lifecycle and evidence. Derived indexes are optional and rebuildable.
- Put behavior in one owning module. Host adapters translate requests and bind trusted identity; they do not implement another copy of Core policy.
- Extend or compose an existing implementation before adding another engine. Remove superseded production paths in the same change; preserve historical fixtures outside the shipping allowlist.
- Keep migration orchestration separate from legacy conversion, host activation and background indexing. A host integration must explicitly prove its writer/activation boundary.
- Multi-hop queries are read-only evidence joins. They must not infer identity, broaden audiences, resurrect invalid facts or promote query-time conclusions.
- Long work needs bounded stages, durable progress, idempotent retry and visible failure. Never turn unknown data into a successful migration or retry forever.
- A new public capability needs a shared contract, both applicable host surfaces, concise operator documentation and focused boundary tests. Avoid full-suite repetition without a specific reason.
- Do not add a dependency or an extensibility layer solely for hypothetical future use. Document the reason when a new durable state or abstraction is necessary.
