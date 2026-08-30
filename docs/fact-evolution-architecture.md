# Fact Evolution, Temporal Ledger, and Reflection Architecture

This document defines the product architecture and safety boundaries for the optional temporal-fact and reflection surfaces. All new behavior is feature-gated and disabled by default.

## 1. Goals

Scope Recall should be able to:

1. recognize that a new factual candidate updates, supersedes, invalidates, coexists with, or merely resembles an existing fact;
2. preserve transaction-time and valid-time history instead of overwriting the only copy;
3. query the current belief, an `as_of` view, and a cited history;
4. generate cross-memory reflection proposals without silently mutating durable memory;
5. keep existing stores and recall behavior compatible while every new write path remains disabled by default.

## 2. Source-of-truth hierarchy

1. SQLite memory and fact-ledger rows are authoritative.
2. The temporal fact ledger records assertion history and action provenance; it never replaces the original memory row or evidence text.
3. Graph relations, FTS, vector indexes, digests, and rendered contexts are rebuildable companion surfaces.
4. LLM output, heuristic extraction, and metadata are proposals or evidence. They are never sufficient authority for an unreviewed destructive action.
5. A current fact is a query result over append-only assertions and their intervals, not the consequence of deleting history.

## 3. Module ownership

### `fact_identity.py` — pure fact identity

Owns:

- canonical subject/predicate/object normalization;
- stable `fact_key` construction and collision-safe fingerprints;
- language-neutral identity helpers;
- parsing structured fact candidates already supplied by deterministic or bounded extractors.

Must not:

- open SQLite connections;
- inspect provider state;
- perform lifecycle, freshness, graph, vector, or audit writes;
- call an LLM.

### `fact_actions.py` — pure action contract and planner

Owns:

- `FactActionKind`: `noop`, `add`, `enrich`, `supersede`, `retract`, `review`;
- immutable candidate, evidence, decision, and action dataclasses;
- deterministic precondition validation;
- conservative rule-based action planning from a candidate plus an explicit existing-fact snapshot;
- stable serialization used by tools, audits, tests, and optional model adapters.

Must not:

- mutate a database;
- infer access scope from global/provider state;
- accept free-form action names;
- downgrade `review` to a write action.

### `temporal_facts.py` — additive schema and status

Owns:

- additive SQLite schema and migrations for fact assertions and evidence links;
- schema indexes and constraints;
- read-only schema-integrity reports.

Must not:

- choose or execute actions;
- commit or roll back a caller-owned transaction;
- import provider/tooling/journal modules.

`sql_store.ensure_schema()` may call `ensure_temporal_fact_schema(conn)`.

### `temporal_query.py` — read-only semantic-time views

Owns:

- timezone-aware query-instant normalization, including fail-closed DST ambiguity handling;
- current cross-scope claim projections joined to recall-visible memory rows;
- bounded lexical matching and evidence counts for recall integration.

`valid_from` is inclusive and `valid_to` is exclusive. The service must remain read-only, scope-filtered, and disabled at runtime unless `temporal_queries.enabled` is true.

### `fact_repository.py` — transaction-neutral temporal repository

Owns:

- assertion creation, evidence linking, and CAS interval closing primitives that do not commit;
- fact-ownership inspection and the explicit mutation-authority guard used by legacy repositories and lifecycle services;
- transaction time (`recorded_at` / `retired_at`) and valid time (`valid_from` / `valid_to`);
- full successor-chain validation before mutation: no self/arbitrary cycle, dangling target, cross-scope/fact edge, or illegal retired target; import/migration callers use this same checker;
- Claim close CAS over claim ID, expected status/retired state, scope, and fact identity; the only missing-successor exception is the Fact Executor's identity-bound pending successor inside one deferred-FK transaction;
- read-only `current`, `as_of`, and `history` queries;
- scope, interval, cardinality, timestamp, and row-to-contract validation.

Must not:

- choose which action is appropriate;
- commit or roll back a caller-owned transaction;
- mutate freshness, graph, FTS, vector, or audit surfaces;
- allow a legacy update, archive, merge, or delete to mutate a memory that owns any fact claim;
- import provider/tooling/journal modules.

### `fact_evidence.py` — source-specific evidence support

Owns deterministic, conservative checks that an authoritative quote itself anchors the proposed claim value, token/entity-bounded subject (or direct first-person speaker), and an ordered subject-predicate-value frame. Latin predicate frames retain prepositions and argument roles; only explicit relation-specific alternates such as `moved to` for a residence update are eligible, while broad lexical families are recall hints rather than write authority. CJK arguments require conservative contiguous boundaries. Negated, uncertain, or semantically ambiguous value clauses fail closed. RETRACT uses a separate correction validator against the ledger-owned target claim; an unrelated direct quote is never a correction. Batch-local model or assistant text can never make an unrelated user quote authoritative.

### `evolution_policy.py` — pure evidence authorization

Owns:

- deterministic source-quality, per-quote claim support, independence, confidence, target, and safety gates;
- stable risk tiers, reason codes, effective actions, and REVIEW downgrade;
- the rule that a model proposal cannot authorize itself.

Must not:

- query or mutate SQLite;
- call a provider;
- upgrade REVIEW into a write action.

### `fact_executor.py` — sole cross-surface mutation coordinator

Owns:

- application of one validated `FactAction` under `BEGIN IMMEDIATE` or a caller-owned savepoint;
- optimistic preconditions (`expected_updated_at`, expected current assertion, scope/access checks);
- exact Claim close preconditions (expected Claim IDs, status/retired state, scope, and fact identity) plus the transaction-local pending-successor authority;
- atomic coordination of temporal assertions, memory lifecycle/metadata, freshness, relations, governance audit, and durable vector outbox intents;
- idempotency and replay-safe receipts;
- rollback on any mandatory-surface failure;
- explicit partial/deferred status only for rebuildable companion work already represented by durable outbox state.

Must not:

- invent actions;
- bypass writable-scope checks;
- hard-delete fact history;
- call model providers;
- commit a caller-owned outer transaction.

### `digest_pollution.py` — pure digest anti-pollution gate

Owns:

- deterministic task/test/repository/historical snapshot classification;
- same-batch factual claim anchoring plus source-specific authoritative quote support;
- conflicting single-value claim detection;
- bounded quarantine reason codes.

Must not write storage, call a model, or import runtime facades. Stable procedural gates are distinguished from completed run results. Heuristic workflows remove transient result text before this assessment. Nightly quarantine receipts stay outside ordinary memory/claim surfaces; journal digest reuses its rejection ledger; dry-run persists neither.

### `fact_evolution.py` — orchestration and policy

Owns:

- candidate intake from tool, journal, nightly digest, or deterministic capture adapters;
- canonical target-to-scope routing (`general` local; `user`, `memory`, `project`, and `ops` shared), claim-scope rebinding, and scope-specific target allowlists;
- fact identity resolution and bounded existing-fact lookup;
- invocation of the pure planner;
- policy decision: preview, reviewed apply, or narrowly allowed low-risk auto-apply;
- action receipts with evidence references and reason codes;
- feature gates, confidence thresholds, candidate caps, and observability counters.

Must not:

- perform mutation SQL or own transaction commits; bounded read-only scope/CAS/receipt lookups are allowed;
- directly update memory, freshness, graph, vector, or audit tables;
- turn an LLM suggestion into an applied action without deterministic validation and policy authorization.

### `fact_tooling.py` — optional public-tool envelope adapter

Owns:

- detecting explicit `claim` / `evolution` envelopes while preserving legacy behavior only for memories that do not own fact claims;
- failing legacy update/archive/merge/delete calls closed when the target memory owns temporal fact history;
- binding store/update requests to provider-owned writable scopes, target IDs, and local apply configuration;
- binding maintenance `scope_recall_evolve` proposals to one writable scope, with `dry_run=true` by default and no caller-controlled mode escalation;
- preserving compatible top-level store/update receipt fields around the structured result;
- replay preflight for terminal target lifecycles without bypassing executor request-hash collision checks.

`scope_recall_fact` is a separate read-only dispatcher over `temporal_query.py`. It is exposed only when `temporal_queries.enabled=true`, supports `current`, `as_of`, and `history`, caps results by local configuration, and uses only provider-owned accessible scopes. Public `scope_recall_store` and `scope_recall_update` evidence fields are caller hints: without a runtime-owned evidence registry, their structured fact actions remain preview/review and cannot auto-apply. `scope_recall_evolve` is exposed and callable only when `maintenance_tools_enabled=true`; its separate `maintenance_mode` remains `preview` by default, and a write requires both local `maintenance_mode=reviewed_apply` and explicit `dry_run=false`. `tool_mode` cannot escalate the maintenance lane.

Must not:

- accept caller-controlled apply modes or manufacture reviewed approval;
- silently route procedure/workflow/episodic content into the temporal fact ledger;
- mutate ledger, lifecycle, graph, vector, freshness, or audit tables directly, except bounded read-only target/scope binding queries;
- replace ordinary store/update behavior for non-fact-owned memories when no structured hint was supplied.

### `reflection.py` — read-only evidence collection

Owns:

- bounded selection of ordinary recall rows and current/history fact evidence;
- provider-owned accessible-scope filtering;
- deterministic evidence identifiers, deduplication, item/character budgets, and at most one supplied follow-up retrieval;
- zero-write evidence-pack traces.

Must not:

- write memory rows, fact assertions, relations, skills, files, or configuration;
- execute a proposed action;
- return uncited conclusions as facts;
- cross user, agent, profile, or local-chat scope boundaries.

### `reflection_llm.py` — strict cited synthesis

Owns strict JSON parsing for observations, inferences, uncertainties, answer, citations, and at most one follow-up query. Every citation must resolve to the closed evidence-pack allowlist. Provider/network mechanics remain in `nightly_llm.py`; malformed output, unknown fields, fenced prose, or invented references fail closed.

### `reflection_grounding.py` — citation-bound candidate material

Owns deterministic support checks for the top-level answer and each observation against that fragment's citations. Unsupported new entities, identifiers, or regions fail closed. Durable candidate text is derived only from supported observations; unbounded answer prose, inferences, and uncertainties are never candidate content.

### `reflection_tooling.py` — runtime and candidate gates

Owns the optional `scope_recall_reflect` orchestration, one-hop limit, configured transport resolution, and hidden `mental_model` candidate creation. Candidate writes require explicit `propose_memory=true`, maintenance mode, `reflection.write_candidates=true`, answer/observation grounding, minimum citations, minimum independent provenance roots, and minimum confidence. Candidates are created as `needs_review`; this module never auto-promotes them.

Any later fact apply workflow must pass a proposal back through `fact_actions` and `fact_executor` as a separately authorized operation.

## 4. Existing-module integration boundaries

| Existing module | Allowed integration | Forbidden responsibility |
| --- | --- | --- |
| `provider.py` | construct services; forward lifecycle hooks; expose thin methods | planning, SQL, temporal policy, reflection synthesis |
| `tooling.py` | normalize arguments; dispatch preview/query/reflect calls; serialize contracts | action selection or direct table writes |
| `memory_ops.py` | existing memory CRUD; call executor at one explicit integration point | duplicate temporal schema/planner logic |
| `journal.py` | emit bounded candidates/evidence; atomically bind each fact action/receipt to its source-entry checkpoint | direct supersede/invalidate writes or fact commits separated from source checkpoints |
| `nightly_digest.py` | batch candidate collection; preview/apply according to feature policy | bespoke action semantics |
| `recall.py` | attach read-only current/as-of/history results to recall surfaces | ledger mutation |
| `freshness.py` | freshness metadata and penalties | canonical fact identity or temporal history |
| `relation_extraction.py` / `graph_relations.py` | relation candidates and graph primitives | fact-action authority |
| `lifecycle_service.py` | savepoint-safe lifecycle transitions and outbox intent | deciding fact evolution actions |
| `sql_store.py` | base memory schema/row helpers and schema bootstrap call | temporal business rules |

## 5. Dependency direction

```text
models / fact_identity
        ↓
    fact_actions
        ↓
 evolution_policy
        ↓
temporal_facts → fact_repository    freshness    graph/relation primitives    lifecycle_service
                         \              |                |                         /
                                          fact_executor
                                                ↑
                                         fact_evolution
                                     ↗      ↑       ↖
                           fact_tooling   journal   nightly_digest
                                ↑
                             tooling
                                                ↓
                                          provider wiring

reflection ──read-only──> temporal_query / fact_repository / scoped memory views
reflection_llm ──transport adapter──> nightly_llm
reflection_tooling ──orchestrates──> reflection / reflection_llm / reviewed candidate storage
recall     ──read-only──> fact_repository
```

Rules:

- lower layers never import provider, tooling, journal, nightly digest, or reflection;
- `reflection.py` and `reflection_llm.py` never import `fact_executor.py`;
- `fact_actions.py` and `fact_identity.py` remain importable without SQLite/provider dependencies;
- cycles are architectural failures, not conveniences to solve with late imports.

## 6. Action safety contract

Every action must carry:

- stable action kind and action ID;
- target scope and fact key;
- source candidate memory/assertion ID;
- affected current assertion IDs;
- evidence references with source type and locator;
- confidence, risk, reason codes, and planner version;
- expected row/assertion versions for optimistic concurrency;
- requested policy mode: `preview`, `reviewed_apply`, or `auto_apply`;
- idempotency key derived from stable lane, routed scope, and source identity/content digest; scheduler `run_id` is audit metadata and never part of replay identity.

Conservative defaults:

- ambiguous identity, contradictory evidence, missing scope, missing preconditions, or unsupported validator → `review`;
- `add` preserves existing assertions for new or explicitly multi-valued slots and records the coexistence reason;
- `retract` closes validity without claiming a replacement;
- `supersede` closes the prior assertion and links the successor;
- no action hard-deletes evidence or temporal history.

## 7. Double-temporal contract

Each assertion records:

- transaction time: when Scope Recall learned/recorded and, if applicable, closed the assertion;
- valid time: when the assertion is believed to apply in the external domain;
- source memory and evidence links;
- lifecycle/status and successor/predecessor links;
- confidence and review state.

Query semantics:

- `current`: open transaction interval and valid at the requested effective time;
- `as_of`: only knowledge recorded by the requested transaction timestamp, evaluated at an optional effective timestamp;
- `history`: ordered assertion/action timeline with citations and closure reasons.

The 1.8 execution contract does not pretend that a static lifecycle flag is a scheduler. Until scheduled lifecycle transitions and interval-aware current uniqueness are implemented, writes fail closed when an `ADD` starts in the future, an `ADD` or successor has a finite `valid_to`, a `SUPERSEDE` starts in the future, or a RETRACT boundary is in the future. RETRACT without an explicit trusted boundary uses the transaction timestamp; a trusted past boundary remains visible in recorded-as-of views from before the correction was learned.

Unknown assertion bounds remain `NULL` unless an action supplies their semantics. A RETRACT is an explicit closure event, so its documented default boundary is the transaction timestamp rather than an unknown bound.

## 8. Reflection v1 contract

Reflection v1 returns separate observations, inferences, uncertainties, a bounded answer, citations, and at most one follow-up query. It has strict evidence/item/character/hop/timeout budgets. Empty evidence or unavailable model transport returns an explicit unavailable result rather than a plausible narrative. Candidate writeback is disabled by default and never activates a memory without review. When explicitly enabled, answer and observations must be citation-grounded, source diversity is counted by provenance root rather than derived memory ID, and candidate content contains only grounded observations.

## 9. Feature gates and compatibility

New behavior is off by default:

- `fact_evolution.enabled = false`
- `fact_evolution.mode = preview`
- `temporal_queries.enabled = false`
- `reflection.enabled = false`
- `reflection.write_candidates = false`

With all gates off:

- existing store/update/merge/forget behavior and response keys remain compatible;
- schema additions are additive and idempotent;
- no new background mutation runs;
- current recall ranking does not change;
- no external model/network call is introduced.

## 10. Verification gates

Each implementation batch must include:

1. RED tests for the specific missing behavior;
2. pure contract/identity tests;
3. migration idempotency and old-database compatibility;
4. rollback and optimistic-conflict tests;
5. current/as-of/history truth-table tests;
6. scope-isolation and private-artifact scans;
7. deterministic `benchmark.memory_evolution.py` and `benchmark.reflection.py` thresholds;
8. read-only `doctor_temporal.py` telemetry for claim coverage, interval/provenance integrity, recursive successor-chain corruption, review debt, recent receipts, and mental-model candidates;
9. full pytest, Ruff, Pyright, release checks, golden recall, wheel/sdist scans;
10. isolated-profile rehearsal before any live replacement.

No commit, push, release, installation, runtime switch, service restart, or live-memory mutation is implied by this document.
