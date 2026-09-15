"""SQLite schema definitions and migration SQL for Scope Recall truth and companion tables.

Schema changes must stay synchronized with migration ledger constants and release tests."""

MAX_MEMORY_IDS_PER_REQUEST = 1000
MAX_MEMORY_ID_LENGTH = 512
DEFAULT_EVIDENCE_DIVERSITY_DEPTH = 3
MAX_EVIDENCE_DIVERSITY_DEPTH = 6

FACT_CLAIM_HINT_SCHEMA = {
    "type": "object",
    "description": "Optional structured factual claim. Scope is always bound by the runtime.",
    "properties": {
        "subject": {"type": "string", "maxLength": 200},
        "predicate": {"type": "string", "maxLength": 120},
        "value": {
            "type": "string",
            "description": (
                "Fact value. The runtime enforces its bounded fact-value limit; "
                "maxLength is intentionally omitted because llama.cpp expands "
                "nested long-string limits into an unparseable grammar."
            ),
        },
        "cardinality": {
            "type": "string",
            "enum": ["single", "multi", "multiple", "many"],
        },
        "valid_from": {"type": "string", "maxLength": 64},
        "valid_to": {"type": "string", "maxLength": 64},
    },
    "required": ["subject", "predicate", "value"],
}

FACT_EVOLUTION_HINT_SCHEMA = {
    "type": "object",
    "description": (
        "Optional fact-evolution proposal. Apply mode is controlled only by trusted "
        "local configuration; tool arguments cannot elevate it."
    ),
    "properties": {
        "action": {
            "type": "string",
            "enum": ["noop", "add", "enrich", "supersede", "retract", "review"],
        },
        "target_ids": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "maxLength": 160},
        },
        "evidence": {
            "type": "array",
            "maxItems": 32,
            "items": {
                "type": "object",
                "properties": {
                    "source_type": {"type": "string", "maxLength": 64},
                    "source_id": {"type": "string", "maxLength": 160},
                    "quote": {"type": "string", "maxLength": 800},
                },
                "required": ["source_type", "source_id"],
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "maxLength": 500},
        "existing_hint": {"type": "string", "maxLength": 1000},
        "idempotency_key": {"type": "string", "maxLength": 200},
    },
}


FACT_EVOLUTION_PROPOSAL_SCHEMA = {
    "type": "object",
    "description": "Evidence-bound evolution proposal; runtime binds scope and execution policy.",
    "properties": {
        **FACT_EVOLUTION_HINT_SCHEMA["properties"],
        "claim": FACT_CLAIM_HINT_SCHEMA,
        "content": {
            "type": "string",
            "description": (
                "Optional memory text. Runtime fact tooling enforces the content "
                "limit; maxLength is omitted for llama.cpp grammar compatibility."
            ),
        },
        "target": {
            "type": "string",
            "enum": ["user", "memory", "project", "ops"],
        },
        "memory_type": {
            "type": "string",
            "enum": ["factual", "preference", "project", "resource", "constraint"],
        },
    },
    "required": ["action"],
}


SCOPE_RECALL_FACT_SCHEMA = {
    "name": "scope_recall_fact",
    "description": "Read one scoped fact slot in current, as-of, or full-history mode.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["current", "as_of", "history"],
            },
            "subject": {"type": "string", "maxLength": 200},
            "predicate": {"type": "string", "maxLength": 120},
            "at": {
                "type": "string",
                "maxLength": 64,
                "description": "ISO-8601 semantic instant; required for as_of.",
            },
            "known_at": {
                "type": "string",
                "maxLength": 64,
                "description": "Optional recorded-time cutoff for delayed-ingestion as_of queries.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["action", "subject", "predicate"],
    },
}


SCOPE_RECALL_EVOLVE_SCHEMA = {
    "name": "scope_recall_evolve",
    "description": "Maintenance review surface for a structured fact proposal; dry-run is the default.",
    "parameters": {
        "type": "object",
        "properties": {
            "proposal": FACT_EVOLUTION_PROPOSAL_SCHEMA,
            "dry_run": {
                "type": "boolean",
                "default": True,
                "description": "Must be explicitly false to request reviewed apply.",
            },
        },
        "required": ["proposal"],
    },
}


SCOPE_RECALL_REFLECT_SCHEMA = {
    "name": "scope_recall_reflect",
    "description": (
        "Run bounded, citation-grounded cross-memory reflection. Read-only by "
        "default; proposing a hidden mental-model candidate requires explicit "
        "maintenance and reflection write gates."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 1000},
            "budget": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_evidence": {"type": "integer", "minimum": 1, "maximum": 64},
                    "max_chars": {"type": "integer", "minimum": 128, "maximum": 50000},
                    "max_item_chars": {"type": "integer", "minimum": 40, "maximum": 4000},
                    "recall_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "fact_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
            "include_trace": {"type": "boolean", "default": False},
            "propose_memory": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Request a hidden needs_review mental-model candidate. "
                    "Requires maintenance_tools_enabled and reflection.write_candidates."
                ),
            },
        },
        "required": ["query"],
    },
}


SCOPE_RECALL_STORE_SCHEMA = {
    "name": "scope_recall_store",
    "description": (
        "Store one atomic fact or one cohesive memory topic. Use separate calls "
        "for unrelated claims; durable targets are user/memory/project/ops, and "
        "general is local scratch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "One atomic fact or one cohesive procedure/topic. Split unrelated "
                    "claims into separate calls so freshness and lifecycle remain "
                    "independently governable."
                ),
            },
            "target": {
                "type": "string",
                "description": "Category; general stays local.",
                "enum": ["user", "memory", "project", "ops", "general"],
            },
            "scope_mode": {
                "type": "string",
                "description": (
                    "Optional write scope selection. It cannot override target policy: "
                    "general is local; durable targets are shared or explicitly shared_pool."
                ),
                "enum": ["shared", "local", "shared_pool"],
            },
            "semantic_merge": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Opt in to conservative contained-text merge. Similar paraphrases and "
                    "changed values remain separate; exact duplicates are always suppressed."
                ),
            },
            "memory_type": {
                "type": "string",
                "description": "Semantic type for governance/ranking.",
                "enum": [
                    "factual",
                    "preference",
                    "procedure",
                    "workflow",
                    "tool_trace",
                    "project",
                    "summary",
                    "pitfall",
                    "decision",
                    "episodic",
                    "resource",
                    "constraint",
                ],
            },
            "importance": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Optional 0..1 importance hint.",
            },
            "freshness": {
                "type": "object",
                "description": "Optional factual freshness evidence. Unspecified factual memories default to needs_live_check.",
                "properties": {
                    "fact_key": {"type": "string"},
                    "truth_type": {"type": "string"},
                    "validator_kind": {
                        "type": "string",
                        "enum": ["manual", "none", "file_exists", "command", "http"],
                    },
                    "validator_spec": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                    "ttl_days": {"type": "integer", "minimum": 0},
                    "last_checked_at": {"type": "string"},
                    "valid_until": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["current", "needs_live_check", "stale", "expired"],
                    },
                    "stale_reason": {"type": "string"},
                    "superseded_by": {"type": "string"},
                },
            },
            "entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Named entities.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags for filtering/audit.",
            },
            "claim": FACT_CLAIM_HINT_SCHEMA,
            "evolution": FACT_EVOLUTION_HINT_SCHEMA,
        },
        "required": ["content"],
    },
}

SCOPE_RECALL_STORE_SECRET_INDEX_SCHEMA = {
    "name": "scope_recall_store_secret_index",
    "description": (
        "Store a searchable secret/credential index without storing plaintext secret material. "
        "Put the actual password/token/key in an external vault/keyring and store only vault_ref plus safe metadata here."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "Human-readable credential label or purpose."},
            "secret_type": {
                "type": "string",
                "description": "Kind of secret being indexed.",
                "enum": ["password", "token", "api_key", "private_key", "cookie", "credential", "other"],
            },
            "service": {"type": "string", "description": "Service, host, app, or integration this credential belongs to."},
            "account": {"type": "string", "description": "Account or principal name, if safe to index."},
            "username": {"type": "string", "description": "Username, if safe to index."},
            "hostname": {"type": "string", "description": "Host or machine name, if relevant."},
            "vault_ref": {"type": "string", "description": "External vault/keyring reference where the plaintext secret is stored."},
            "secret_value": {
                "type": "string",
                "description": "Optional plaintext supplied only to compute a short fingerprint; it is never stored in SQL/FTS/vector.",
            },
            "notes": {"type": "string", "description": "Safe notes. Any secret-looking assignments are redacted before storage."},
            "rotation_due": {"type": "string", "description": "Optional rotation/review date or cadence."},
            "target": {"type": "string", "enum": ["memory", "project", "ops"], "description": "Durable target; defaults to ops."},
            "entities": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["label"],
    },
}

SCOPE_RECALL_SEARCH_SCHEMA = {
    "name": "scope_recall_search",
    "description": "Search accessible Scope Recall memories.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "query_variants": {
                "type": "array",
                "maxItems": 7,
                "items": {"type": "string", "maxLength": 1000},
                "description": (
                    "Optional explicit query variants for deterministic multi-hop "
                    "evidence-set fusion. The primary query is always included."
                ),
            },
            "evidence_diversity_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_EVIDENCE_DIVERSITY_DEPTH,
                "default": DEFAULT_EVIDENCE_DIVERSITY_DEPTH,
                "description": (
                    "Per-query specialist hits protected before global RRF fill. "
                    "Use 4-6 only for broad multi-hop or open-domain evidence sets."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Max results.",
            },
            "recall_mode": {
                "type": "string",
                "enum": ["advisory", "strict"],
                "description": (
                    "advisory returns stale evidence with warnings; strict excludes "
                    "stale and expired rows."
                ),
            },
            "include_trace": {"type": "boolean", "description": "Include Recall Funnel trace."},
        },
        "required": ["query"],
    },
}

SCOPE_RECALL_INSPECTOR_SCHEMA = {
    "name": "scope_recall_inspector",
    "description": (
        "Explain the exact production Recall Packet for one read-only search, "
        "including provenance, truth state, token cost, confidence, timeline, "
        "and non-executing correction/archive/purge plans."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 5,
            },
            "recall_mode": {
                "type": "string",
                "enum": ["advisory", "strict"],
                "default": "advisory",
            },
            "include_content": {
                "type": "boolean",
                "default": False,
                "description": "Include sanitized memory content; summaries are always shown.",
            },
            "format": {
                "type": "string",
                "enum": ["json", "text"],
                "default": "json",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

SCOPE_RECALL_MEMORY_SCHEMA = {
    "name": "scope_recall_memory",
    "description": "Memory operations by exact id. Candidate promote/archive: preview by default; dry_run=false applies the plan.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["inspect", "feedback", "update", "merge", "forget", "promote", "archive"]},
            "dry_run": {"type": "boolean", "default": True},
            "expected_updated_at": {"type": "string", "description": "Review revision; rejects stale plans."},
            "expected_lifecycle": {"type": "string"},
            "id": {"type": "string", "maxLength": MAX_MEMORY_ID_LENGTH},
            "ids": {
                "type": "array",
                "maxItems": MAX_MEMORY_IDS_PER_REQUEST,
                "items": {"type": "string", "maxLength": MAX_MEMORY_ID_LENGTH},
                "description": "Memory ids for forget.",
            },
            "rating": {"type": "string"},
            "note": {"type": "string"},
            "content": {"type": "string", "description": "Replacement or merged content."},
            "target": {"type": "string", "enum": ["user", "memory", "project", "ops", "general"]},
            "target_id": {"type": "string", "maxLength": MAX_MEMORY_ID_LENGTH, "description": "Merge target id."},
            "source_ids": {
                "type": "array",
                "maxItems": MAX_MEMORY_IDS_PER_REQUEST,
                "items": {"type": "string", "maxLength": MAX_MEMORY_ID_LENGTH},
                "description": "Merge source ids.",
            },
            "source_candidate_id": {"type": "string", "description": "Optional merge audit candidate id."},
            "memory_type": {
                "type": "string",
                "description": "Structured update semantic type.",
            },
            "claim": FACT_CLAIM_HINT_SCHEMA,
            "evolution": FACT_EVOLUTION_HINT_SCHEMA,
        },
        "required": ["action"],
    },
}

SCOPE_RECALL_ENTITY_SCHEMA = {
    "name": "scope_recall_entity",
    "description": "Compact entity graph operations: probe memories for an entity or list related entities.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["probe", "related"], "description": "Entity operation."},
            "entity": {"type": "string", "description": "Entity/person/project/service."},
            "limit": {"type": "integer", "description": "Max results."},
        },
        "required": ["action", "entity"],
    },
}

SCOPE_RECALL_FORGET_SCHEMA = {
    "name": "scope_recall_forget",
    "description": "Forget Scope Recall memories by exact id within the current accessible scope set. Defaults to audited soft archive with a receipt; hard_delete is maintenance-only.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "maxLength": MAX_MEMORY_ID_LENGTH, "description": "Single memory id to forget/archive."},
            "ids": {
                "type": "array",
                "maxItems": MAX_MEMORY_IDS_PER_REQUEST,
                "items": {"type": "string", "maxLength": MAX_MEMORY_ID_LENGTH},
                "description": "Exact memory ids to forget/archive.",
            },
            "reason": {"type": "string", "description": "Operator-readable reason for the audited forget/archive action."},
            "hard_delete": {"type": "boolean", "description": "Maintenance-only: hard delete instead of soft archive."},
        },
    },
}

SCOPE_RECALL_PURGE_SCHEMA = {
    "name": "scope_recall_purge",
    "description": (
        "Explicit two-phase privacy purge for exact memory ids. Maintenance-only: "
        "plan is zero-write, deny commits an irreversible visibility tombstone, and "
        "erase performs idempotent physical removal after a second confirmation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["plan", "status", "deny", "erase"],
            },
            "id": {
                "type": "string",
                "maxLength": MAX_MEMORY_ID_LENGTH,
                "description": "One exact memory id for plan or deny.",
            },
            "ids": {
                "type": "array",
                "maxItems": MAX_MEMORY_IDS_PER_REQUEST,
                "items": {"type": "string", "maxLength": MAX_MEMORY_ID_LENGTH},
                "description": "Exact memory ids for plan or deny.",
            },
            "operation_id": {
                "type": "string",
                "maxLength": 96,
                "description": "Plan-generated operation id; required after plan.",
            },
            "confirmation": {
                "type": "string",
                "maxLength": 64,
                "description": "Exact phase confirmation returned by plan or deny.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

SCOPE_RECALL_UPDATE_SCHEMA = {
    "name": "scope_recall_update",
    "description": "Update a Scope Recall memory by id within the current accessible scope set.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Memory id to update."},
            "content": {"type": "string", "description": "Replacement memory text."},
            "target": {
                "type": "string",
                "description": "Optional replacement category.",
                "enum": ["user", "memory", "project", "ops", "general"],
            },
            "memory_type": {
                "type": "string",
                "description": "Optional semantic type for a structured factual update.",
            },
            "claim": FACT_CLAIM_HINT_SCHEMA,
            "evolution": FACT_EVOLUTION_HINT_SCHEMA,
        },
        "required": ["id", "content"],
    },
}

SCOPE_RECALL_DEDUPE_SCHEMA = {
    "name": "scope_recall_dedupe",
    "description": "Find or collapse exact duplicate Scope Recall memories. Operator-only: requires maintenance_tools_enabled=true.",
    "parameters": {
        "type": "object",
        "properties": {
            "dry_run": {"type": "boolean", "description": "Inspect only; default true."},
            "scope_only": {"type": "boolean", "description": "Restrict dedupe to the current accessible scope set."},
        },
    },
}

SCOPE_RECALL_HYGIENE_SCHEMA = {
    "name": "scope_recall_hygiene",
    "description": "Build a read-only Scope Recall memory hygiene report. Operator-only: requires maintenance_tools_enabled=true.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Maximum examples per report category; default 200."},
        },
    },
}

SCOPE_RECALL_MERGE_SCHEMA = {
    "name": "scope_recall_merge",
    "description": "Merge one or more Scope Recall memories into a target memory.",
    "parameters": {
        "type": "object",
        "properties": {
            "target_id": {
                "type": "string",
                "maxLength": MAX_MEMORY_ID_LENGTH,
                "description": "Memory id to keep/update.",
            },
            "source_ids": {
                "type": "array",
                "maxItems": MAX_MEMORY_IDS_PER_REQUEST,
                "items": {"type": "string", "maxLength": MAX_MEMORY_ID_LENGTH},
                "description": "Memory ids to merge then delete.",
            },
            "content": {"type": "string", "description": "Optional explicit merged content."},
            "target": {"type": "string", "enum": ["user", "memory", "project", "ops", "general"]},
            "source_candidate_id": {"type": "string", "description": "Optional audit candidate id to include in the merge receipt."},
        },
        "required": ["target_id"],
    },
}

SCOPE_RECALL_EXPORT_SCHEMA = {
    "name": "scope_recall_export",
    "description": "Export SQLite truth rows as JSON or JSONL. Defaults to the current accessible scope set; scope_only=false requires maintenance_tools_enabled=true.",
    "parameters": {
        "type": "object",
        "properties": {
            "format": {"type": "string", "enum": ["jsonl", "json"], "description": "Export format."},
            "scope_only": {"type": "boolean", "description": "Restrict export to the current accessible scope set; default true."},
        },
    },
}

SCOPE_RECALL_GOVERN_SCHEMA = {
    "name": "scope_recall_govern",
    "description": "Run deterministic memory governance classification and decay review. Operator-only: requires maintenance_tools_enabled=true.",
    "parameters": {
        "type": "object",
        "properties": {
            "dry_run": {"type": "boolean", "description": "Inspect only; default true."},
            "scope_only": {"type": "boolean", "description": "Restrict governance to the current accessible scope set; default true."},
        },
    },
}

SCOPE_RECALL_REPAIR_SCHEMA = {
    "name": "scope_recall_repair",
    "description": "Repair/rebuild the configured vector companion from SQLite truth. Operator-only: requires maintenance_tools_enabled=true.",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    },
}

SCOPE_RECALL_STATS_SCHEMA = {
    "name": "scope_recall_stats",
    "description": "Show Scope Recall storage, retrieval, and scope statistics.",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    },
}

SCOPE_RECALL_INSPECT_SCHEMA = {
    "name": "scope_recall_inspect",
    "description": "Inspect one Scope Recall row with metadata, feedback, and relation evidence.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Memory id to inspect."},
        },
        "required": ["id"],
    },
}

SCOPE_RECALL_EXPLAIN_SCHEMA = {
    "name": "scope_recall_explain",
    "description": "Explain Scope Recall retrieval results with lexical, BM25, vector, RRF, entity, decay, recency, and trust components.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Query to explain."},
            "limit": {"type": "integer", "description": "Maximum results to explain."},
        },
        "required": ["query"],
    },
}

SCOPE_RECALL_BENCHMARK_SCHEMA = {
    "name": "scope_recall_benchmark",
    "description": "Run read-only Scope Recall query latency, Recall Funnel, or assertion regression checks.",
    "parameters": {
        "type": "object",
        "properties": {
            "queries": {
                "anyOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "string"},
                ],
                "description": "Simple query latency checks. Accepts a string or an array of strings.",
            },
            "cases": {
                "type": "array",
                "description": "Assertion cases with query plus optional expected_ids, forbidden_ids, min_rank, and min_top_score.",
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "expected_ids": {
                            "anyOf": [
                                {"type": "array", "items": {"type": "string"}},
                                {"type": "string"},
                            ]
                        },
                        "forbidden_ids": {
                            "anyOf": [
                                {"type": "array", "items": {"type": "string"}},
                                {"type": "string"},
                            ]
                        },
                        "min_rank": {"type": "integer"},
                        "min_top_score": {"type": "number"},
                    },
                    "required": ["query"],
                },
            },
            "auto_explain_on_fail": {"type": "boolean", "description": "Include scope_recall_explain payload for failed assertion cases."},
            "include_trace": {"type": "boolean", "description": "Include per-query Recall Funnel traces in benchmark results."},
            "prompt_budget_chars": {"type": "integer", "description": "Optional returned-character budget used to compute prompt_budget_hit_rate."},
            "limit": {"type": "integer", "description": "Maximum results per query."},
        },
    },
}

SCOPE_RECALL_PLAYBOOK_CREATE_SCHEMA = {
    "name": "scope_recall_playbook_create",
    "description": "Create a procedural playbook candidate row. Maintenance-only; promotion requires scope_recall_playbook_review after independent review.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Optional stable playbook id."},
            "payload": {
                "type": "object",
                "additionalProperties": True,
                "description": "procedural_playbook.v1 payload.",
            },
            "status": {"type": "string", "enum": ["candidate"], "description": "Optional; create only accepts candidate."},
            "confidence": {"type": "number", "description": "Initial confidence 0..1."},
            "created_from_episode_id": {"type": "string"},
            "evidence_anchors": {"type": "array", "items": {}},
            "related_skills": {"type": "array", "items": {"type": "string"}},
            "environment_constraints": {
                "type": "object",
                "additionalProperties": True,
            },
            "metadata": {"type": "object", "additionalProperties": True},
        },
        "required": ["payload"],
    },
}

SCOPE_RECALL_PLAYBOOK_SEARCH_SCHEMA = {
    "name": "scope_recall_playbook_search",
    "description": "Search accessible procedural playbooks by task/query/status. Read-only and scope-filtered before ranking.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Task query or trigger text."},
            "task_class": {"type": "string", "description": "Optional exact task class filter."},
            "status": {"type": "string", "description": "Optional status filter."},
            "limit": {"type": "integer", "description": "Maximum results."},
        },
    },
}

SCOPE_RECALL_PLAYBOOK_INSPECT_SCHEMA = {
    "name": "scope_recall_playbook_inspect",
    "description": "Inspect one accessible procedural playbook with versions and recent runs.",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "Playbook id."}},
        "required": ["id"],
    },
}

SCOPE_RECALL_EXPERIENCE_PREFLIGHT_SCHEMA = {
    "name": "scope_recall_experience_preflight",
    "description": "Render a bounded Experience Kernel packet for a task query. Read-only; runtime injection follows experience.prefetch_enabled and can be disabled in config.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Current task query."},
            "limit": {"type": "integer", "description": "Candidate playbook limit."},
        },
        "required": ["query"],
    },
}

SCOPE_RECALL_PLAYBOOK_FEEDBACK_SCHEMA = {
    "name": "scope_recall_playbook_feedback",
    "description": "Record outcome feedback for a playbook run and update counters/confidence/status.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Playbook id."},
            "run_id": {
                "type": "string",
                "description": "Optional pending run id returned by preflight; finalizes that run instead of creating a second row.",
            },
            "outcome": {"type": "string", "enum": ["success", "partial", "failed", "stale", "misleading", "unknown"]},
            "decision": {"type": "string", "enum": ["direct_reuse", "guided_reuse", "no_reuse"]},
            "evidence": {"type": "array", "items": {}},
            "preconditions_checked": {"type": "array", "items": {}, "description": "Optional live-check results captured while reusing the playbook."},
            "steps_completed": {"type": "array", "items": {}, "description": "Optional executed-step results captured while reusing the playbook."},
            "outcome_reason": {"type": "string"},
            "model_name": {"type": "string"},
            "tool_call_count": {"type": "integer"},
            "token_estimate": {"type": "integer"},
        },
        "required": ["id", "outcome"],
    },
}

SCOPE_RECALL_PLAYBOOK_REVIEW_SCHEMA = {
    "name": "scope_recall_playbook_review",
    "description": "Review/promote/quarantine/supersede playbooks, list duplicate groups, or merge duplicate playbooks. Maintenance-only.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Playbook id. For action=merge this is the canonical target id."},
            "target_id": {"type": "string", "description": "Optional canonical target id for action=merge; alias for id."},
            "source_ids": {"type": "array", "items": {"type": "string"}, "description": "Source playbook ids to supersede into target when action=merge."},
            "action": {
                "type": "string",
                "enum": [
                    "review",
                    "reviewed",
                    "promote",
                    "promoted",
                    "needs_review",
                    "quarantine",
                    "quarantined",
                    "supersede",
                    "superseded",
                    "dedupe",
                    "duplicates",
                    "list_duplicates",
                    "merge",
                ],
            },
            "reason": {"type": "string"},
            "superseded_by": {"type": "string"},
            "status": {"type": "string", "description": "Optional duplicate-list status filter."},
            "limit": {"type": "integer", "description": "Maximum duplicate groups to return."},
            "dry_run": {"type": "boolean", "description": "Inspect only for write actions by default; set false to apply promote/quarantine/supersede/merge changes."},
            "force_cross_class": {"type": "boolean", "description": "Allow supersede/merge across mismatched task_class/title only when a non-empty reason documents the operator decision."},
            "validated_payload": {
                "type": "object",
                "additionalProperties": True,
                "description": "Optional prior dry-run result; apply fails with stale_validation if the bound plan or rows changed.",
            },
        },
        "required": ["action"],
    },
}

SCOPE_RECALL_EXPERIENCE_STATS_SCHEMA = {
    "name": "scope_recall_experience_stats",
    "description": "Show Experience Kernel playbook/run counts for accessible scopes.",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    },
}

SCOPE_RECALL_EXPERIENCE_PROMOTE_SCHEMA = {
    "name": "scope_recall_experience_promote",
    "description": "自动从 journal 任务轨迹中提取可复用经验手册。维护工具；默认 dry-run，不要求用户人工逐条复审。",
    "parameters": {
        "type": "object",
        "properties": {
            "dry_run": {"type": "boolean", "description": "Inspect only; default true."},
            "limit_sessions": {"type": "integer", "description": "Maximum recent sessions to inspect."},
        },
    },
}

SCOPE_RECALL_FORGETTING_REPORT_SCHEMA = {
    "name": "scope_recall_forgetting_report",
    "description": "生成只读遗忘/归档报告，找出重复、低价值、运行噪声和疑似敏感记忆。维护工具。",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Maximum examples per report category; default 200."},
        },
    },
}

SCOPE_RECALL_FORGETTING_RUN_SCHEMA = {
    "name": "scope_recall_forgetting_run",
    "description": "执行遗忘机制。默认 dry-run；非 dry-run 默认软归档，不物理删除普通记忆。维护工具。",
    "parameters": {
        "type": "object",
        "properties": {
            "dry_run": {"type": "boolean", "description": "Inspect only; default true."},
            "soft_archive": {
                "type": "boolean",
                "description": "Override forgetting.soft_archive_default for this run.",
            },
            "hard_delete": {
                "type": "boolean",
                "description": (
                    "Request hard delete for explicit candidates; also requires "
                    "forgetting.hard_delete_sensitive=true."
                ),
            },
            "limit": {"type": "integer", "description": "Maximum candidates to process."},
        },
    },
}

SCOPE_RECALL_CONTEXT_SCHEMA = {
    "name": "scope_recall_context",
    "description": "Build a compact task-relevant memory context block plus structured evidence for a query.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Current task or question."},
            "limit": {"type": "integer", "description": "Maximum memories to include."},
            "max_chars": {"type": "integer", "description": "Maximum characters for the rendered context block."},
        },
        "required": ["query"],
    },
}

SCOPE_RECALL_PROFILE_SCHEMA = {
    "name": "scope_recall_profile",
    "description": (
        "Build a compact high-level Scope Recall profile/context surface from accessible durable memory, "
        "optional local scratch, and live curated USER/MEMORY files without exposing raw journal rows."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Optional current task/query used to select project/ops/context rows."},
            "entity": {"type": "string", "description": "Optional entity/person/project to focus project/ops/context rows."},
            "targets": {
                "type": "array",
                "items": {"type": "string", "enum": ["user", "memory", "project", "ops", "general"]},
                "description": "Optional target sections to include. Defaults to user/memory/project/ops; general requires include_general=true or explicit target.",
            },
            "include_general": {"type": "boolean", "description": "Include current local general scratch/session rows; default false."},
            "include_candidates": {"type": "boolean", "description": "Include non-hidden candidate SQLite rows in addition to promoted profile rows; default false."},
            "include_curated": {"type": "boolean", "description": "Include live Hermes USER.md/MEMORY.md entries when curated-memory policy allows it; default true."},
            "limit": {"type": "integer", "description": "Maximum memories per section."},
            "max_chars": {"type": "integer", "description": "Maximum characters for the rendered compact profile/context block."},
        },
    },
}

SCOPE_RECALL_PROBE_SCHEMA = {
    "name": "scope_recall_probe",
    "description": "Probe all accessible Scope Recall memories attached to an entity.",
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "Entity name, person, project, service, or identifier."},
            "limit": {"type": "integer", "description": "Maximum memories to return."},
        },
        "required": ["entity"],
    },
}

SCOPE_RECALL_RELATED_SCHEMA = {
    "name": "scope_recall_related",
    "description": "List entities that co-occur with a given entity in accessible memories.",
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "Entity to expand from."},
            "limit": {"type": "integer", "description": "Maximum related entities to return."},
        },
        "required": ["entity"],
    },
}

SCOPE_RECALL_FEEDBACK_SCHEMA = {
    "name": "scope_recall_feedback",
    "description": "Mark an accessible memory as helpful or unhelpful so future recall can adjust trust.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Memory id to rate."},
            "rating": {
                "type": "string",
                "description": "Feedback rating.",
                "enum": ["helpful", "unhelpful", "up", "down", "1", "-1"],
            },
            "note": {"type": "string", "description": "Optional short audit note."},
        },
        "required": ["id", "rating"],
    },
}
