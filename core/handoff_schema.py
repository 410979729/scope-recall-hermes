"""SQLite task-report tables, separate from facts and extraction work."""

STATEMENTS = (
    """CREATE TABLE handoffs (
        handoff_id TEXT PRIMARY KEY NOT NULL, task_ref TEXT NOT NULL REFERENCES episodes(episode_id),
        task_revision INTEGER NOT NULL, scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id),
        project_id TEXT, branch_id TEXT, project_key TEXT, sender_entry TEXT NOT NULL,
        sender_session TEXT, sender_project_root TEXT,
        sender_project_identity TEXT NOT NULL DEFAULT 'local'
            CHECK(sender_project_identity IN ('local','remote','unverified')),
        session_label TEXT, confirmed INTEGER NOT NULL CHECK(confirmed IN (0,1)),
        state TEXT NOT NULL CHECK(state IN ('pending','accepted','cancelled','expired')),
        revision INTEGER NOT NULL CHECK(revision>=1), resume_json TEXT NOT NULL CHECK(json_valid(resume_json)),
        safety_state TEXT NOT NULL CHECK(safety_state IN ('released','quarantined','pending_review')),
        rules_json TEXT NOT NULL CHECK(json_valid(rules_json)), created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL, accepted_entry TEXT, draft_key TEXT UNIQUE
    ) STRICT""",
    "CREATE INDEX handoff_pending ON handoffs(scope_id,project_key,branch_id,state,created_at)",
    """CREATE TABLE handoff_events (
        handoff_id TEXT NOT NULL REFERENCES handoffs(handoff_id), revision INTEGER NOT NULL,
        action TEXT NOT NULL, actor_entry TEXT NOT NULL, recorded_at TEXT NOT NULL,
        PRIMARY KEY(handoff_id,revision)
    ) STRICT, WITHOUT ROWID""",
)
