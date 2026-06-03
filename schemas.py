SCOPE_RECALL_STORE_SCHEMA = {
    "name": "scope_recall_store",
    "description": "Store a Scope Recall memory. user/memory/project/ops targets are durable shared memories; general is local scratch.",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Memory text to store."},
            "target": {
                "type": "string",
                "description": "Category. user/memory/project/ops are shared durable; general stays local to the current chat/thread/session.",
                "enum": ["user", "memory", "project", "ops", "general"],
            },
            "memory_type": {
                "type": "string",
                "description": "Optional semantic type used for governance and ranking.",
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
                "description": "Optional 0..1 importance hint. Higher values are mildly favored in recall.",
            },
            "entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional named entities to attach to this memory.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for filtering and audit.",
            },
        },
        "required": ["content"],
    },
}

SCOPE_RECALL_SEARCH_SCHEMA = {
    "name": "scope_recall_search",
    "description": "Search Scope Recall memories relevant to a query across the current local scope plus shared durable scope.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "limit": {"type": "integer", "description": "Maximum results to return."},
        },
        "required": ["query"],
    },
}

SCOPE_RECALL_FORGET_SCHEMA = {
    "name": "scope_recall_forget",
    "description": "Delete Scope Recall memories matching a query within the current accessible scope set.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Query used to find memories to delete."},
            "limit": {"type": "integer", "description": "Maximum matching memories to delete."},
        },
        "required": ["query"],
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
            "target_id": {"type": "string", "description": "Memory id to keep/update."},
            "source_ids": {"type": "array", "items": {"type": "string"}, "description": "Memory ids to merge then delete."},
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
    "description": "Repair/rebuild the LanceDB vector companion from SQLite truth. Operator-only: requires maintenance_tools_enabled=true.",
    "parameters": {"type": "object", "properties": {}},
}

SCOPE_RECALL_STATS_SCHEMA = {
    "name": "scope_recall_stats",
    "description": "Show Scope Recall storage, retrieval, and scope statistics.",
    "parameters": {"type": "object", "properties": {}},
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
    "description": "Explain Scope Recall retrieval results with lexical, BM25, vector, decay, and trust components.",
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
    "description": "Run read-only Scope Recall query latency smoke checks.",
    "parameters": {
        "type": "object",
        "properties": {
            "queries": {"type": "array", "items": {"type": "string"}, "description": "Queries to benchmark."},
            "limit": {"type": "integer", "description": "Maximum results per query."},
        },
        "required": ["queries"],
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
