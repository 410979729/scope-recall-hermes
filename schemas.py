SCOPE_RECALL_STORE_SCHEMA = {
    "name": "scope_recall_store",
    "description": "Store a durable memory in the local Scope Recall provider.",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Memory text to store."},
            "target": {
                "type": "string",
                "description": "Optional category such as user or memory.",
                "enum": ["user", "memory", "project", "ops", "general"],
            },
        },
        "required": ["content"],
    },
}

SCOPE_RECALL_SEARCH_SCHEMA = {
    "name": "scope_recall_search",
    "description": "Search local Scope Recall memories relevant to a query.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "limit": {"type": "integer", "description": "Maximum results to return."},
        },
        "required": ["query"],
    },
}

SCOPE_RECALL_STATS_SCHEMA = {
    "name": "scope_recall_stats",
    "description": "Show Scope Recall storage, retrieval, and scope statistics.",
    "parameters": {"type": "object", "properties": {}},
}
