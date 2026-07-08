CREATE TABLE IF NOT EXISTS scope_recall_shared_memories (
    id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    target TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    conflict_policy TEXT NOT NULL,
    source_scope_id TEXT NOT NULL DEFAULT '',
    source_updated_at TEXT NOT NULL DEFAULT '',
    source_trust DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS scope_recall_shared_memories_target_idx
    ON scope_recall_shared_memories(target);

CREATE INDEX IF NOT EXISTS scope_recall_shared_memories_source_scope_idx
    ON scope_recall_shared_memories(source_scope_id);

CREATE INDEX IF NOT EXISTS scope_recall_shared_memories_source_updated_idx
    ON scope_recall_shared_memories(source_updated_at);
