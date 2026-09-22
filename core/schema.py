"""Versioned target DDL. Only explicit initialization/maintenance executes this."""

SCHEMA_VERSION = 1110
#: Schemas a store may carry and be brought forward from, oldest first.  Any
#: other version is unsupported and never touched.
UPGRADE_CHAIN = (1105, 1106, 1107, 1108, 1109)
APPLICATION_ID = 0x5352434C

WORK_ITEMS_STATEMENT = """CREATE TABLE work_items (
        work_id INTEGER PRIMARY KEY,
        work_type TEXT NOT NULL CHECK(work_type IN ('consolidate','embed','rebuild_projection','purge','evaluate_candidate')),
        subject_ref TEXT NOT NULL, subject_revision INTEGER NOT NULL CHECK(subject_revision>=1),
        scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id),
        project_id TEXT, branch_id TEXT,
        state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','leased','done','failed','obsolete')),
        attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt>=0),
        available_at TEXT NOT NULL, lease_token INTEGER NOT NULL DEFAULT 0 CHECK(lease_token>=0),
        lease_owner TEXT, lease_until TEXT, last_error_code TEXT,
        consolidation_offset INTEGER NOT NULL DEFAULT 0 CHECK(consolidation_offset>=0),
        UNIQUE(work_type,subject_ref,subject_revision)
    ) STRICT"""

#: Tool-output vectors the retention window expired (``runtime/vector_retention.py``).
#: The source stays; the row says its vector is gone on purpose, so nothing
#: counts it as missing or embeds it again.
EXPIRED_VECTORS_STATEMENT = """CREATE TABLE expired_vectors (
        source_ref TEXT NOT NULL, source_revision INTEGER NOT NULL CHECK(source_revision>=1),
        expired_at TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT 'window' CHECK(reason IN ('window','repeat','omitted')),
        PRIMARY KEY(source_ref,source_revision)
    ) STRICT, WITHOUT ROWID"""
#: The agents a shared store's sources came in through: each source row carries
#: an ``entry_id``, and this is where that id gets the name a reader is shown.
#: A local store leaves it empty; its rows all say ``local``.
ENTRIES_STATEMENT = """CREATE TABLE entries (
        entry_id TEXT PRIMARY KEY NOT NULL CHECK(length(entry_id) BETWEEN 2 AND 32),
        display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 32),
        host TEXT NOT NULL CHECK(length(host) BETWEEN 1 AND 32),
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
    ) STRICT"""
#: The scope authorization a migrated source was admitted under.  The 2.0
#: conversion wrote the same 600-byte record into every source's extra_json:
#: 97 distinct payloads across 167,000 sources, 102 MB, on one instance.  It is
#: audit evidence, so it is kept once per distinct payload and linked.
AUTHORIZATION_STATEMENTS = (
    """CREATE TABLE authorization_payloads (
        authorization_id INTEGER PRIMARY KEY,
        payload TEXT NOT NULL UNIQUE CHECK(json_valid(payload))
    ) STRICT""",
    """CREATE TABLE source_authorizations (
        event_id TEXT NOT NULL, source_revision INTEGER NOT NULL CHECK(source_revision>=1),
        authorization_id INTEGER NOT NULL REFERENCES authorization_payloads(authorization_id),
        PRIMARY KEY(event_id,source_revision)
    ) STRICT, WITHOUT ROWID""",
)
#: The lexical index (``core/lexical_index.py``): a term dictionary and
#: integer postings.  The text projection it replaces took half of one
#: instance's store; this takes a fifth of that.
LEXICAL_STATEMENTS = (
    """CREATE TABLE lexical_terms (
        term_id INTEGER PRIMARY KEY, term TEXT NOT NULL UNIQUE
    ) STRICT""",
    """CREATE TABLE lexical_postings (
        term_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
        PRIMARY KEY(term_id,source_id)
    ) STRICT, WITHOUT ROWID""",
    "CREATE INDEX lexical_postings_source ON lexical_postings(source_id,term_id)",
)
CANDIDATE_STATEMENTS = (
    """CREATE TABLE candidate_lifecycle (
        candidate_ref TEXT NOT NULL, candidate_revision INTEGER NOT NULL CHECK(candidate_revision>=1),
        scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id), project_id TEXT, branch_id TEXT,
        processing_state TEXT NOT NULL CHECK(processing_state IN
            ('pending_evaluation','waiting_evidence','resolved','archived','blocked')),
        reason TEXT NOT NULL, rule_version TEXT NOT NULL,
        evidence_fingerprint TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        last_evidence_at TEXT, last_evaluated_at TEXT, dormant_at TEXT,
        PRIMARY KEY(candidate_ref,candidate_revision),
        FOREIGN KEY(candidate_ref,candidate_revision) REFERENCES claim_versions(claim_id,revision)
    ) STRICT, WITHOUT ROWID""",
    "CREATE INDEX candidate_state_wait ON candidate_lifecycle(processing_state,updated_at,candidate_ref,candidate_revision)",
    """CREATE TABLE candidate_evidence (
        candidate_ref TEXT NOT NULL, candidate_revision INTEGER NOT NULL,
        source_ref TEXT NOT NULL, source_revision INTEGER NOT NULL CHECK(source_revision>=1),
        observed_at TEXT NOT NULL,
        PRIMARY KEY(candidate_ref,candidate_revision,source_ref,source_revision),
        FOREIGN KEY(candidate_ref,candidate_revision)
            REFERENCES candidate_lifecycle(candidate_ref,candidate_revision) ON DELETE CASCADE,
        FOREIGN KEY(source_ref,source_revision)
            REFERENCES source_events(event_id,source_revision)
    ) STRICT, WITHOUT ROWID""",
    "CREATE INDEX candidate_evidence_source ON candidate_evidence(source_ref,source_revision,candidate_ref,candidate_revision)",
    """CREATE TABLE candidate_trigger_terms (
        term TEXT NOT NULL, candidate_ref TEXT NOT NULL, candidate_revision INTEGER NOT NULL,
        PRIMARY KEY(term,candidate_ref,candidate_revision),
        FOREIGN KEY(candidate_ref,candidate_revision)
            REFERENCES candidate_lifecycle(candidate_ref,candidate_revision) ON DELETE CASCADE
    ) STRICT, WITHOUT ROWID""",
    "CREATE INDEX candidate_terms_owner ON candidate_trigger_terms(candidate_ref,candidate_revision,term)",
    """CREATE TABLE candidate_evaluations (
        evaluation_id INTEGER PRIMARY KEY,
        candidate_ref TEXT NOT NULL, candidate_revision INTEGER NOT NULL CHECK(candidate_revision>=1),
        evidence_fingerprint TEXT NOT NULL, evidence_refs_json TEXT NOT NULL
            CHECK(json_valid(evidence_refs_json) AND length(evidence_refs_json)<=16384),
        rule_version TEXT NOT NULL, memory_epoch INTEGER NOT NULL CHECK(memory_epoch>=0),
        state TEXT NOT NULL CHECK(state IN ('queued','resolved','waiting_evidence','archived','failed','obsolete')),
        reason TEXT NOT NULL, work_id INTEGER UNIQUE REFERENCES work_items(work_id) ON DELETE SET NULL,
        created_at TEXT NOT NULL, completed_at TEXT, model_attempted_at TEXT,
        result_digest TEXT, failure_code TEXT,
        UNIQUE(candidate_ref,candidate_revision,evidence_fingerprint,rule_version),
        FOREIGN KEY(candidate_ref,candidate_revision)
            REFERENCES candidate_lifecycle(candidate_ref,candidate_revision) ON DELETE CASCADE
    ) STRICT""",
    "CREATE INDEX candidate_evaluation_state ON candidate_evaluations(state,created_at,evaluation_id)",
    """CREATE TABLE candidate_source_triggers (
        source_ref TEXT NOT NULL, source_revision INTEGER NOT NULL CHECK(source_revision>=1),
        matched_count INTEGER NOT NULL CHECK(matched_count>=0),
        scheduled_count INTEGER NOT NULL CHECK(scheduled_count>=0),
        truncated INTEGER NOT NULL CHECK(truncated IN (0,1)), processed_at TEXT NOT NULL,
        PRIMARY KEY(source_ref,source_revision),
        FOREIGN KEY(source_ref,source_revision) REFERENCES source_events(event_id,source_revision)
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE candidate_scan_cursors (
        cursor_name TEXT PRIMARY KEY NOT NULL,
        position_ref TEXT, position_revision INTEGER,
        processed_count INTEGER NOT NULL DEFAULT 0 CHECK(processed_count>=0),
        completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1)),
        updated_at TEXT NOT NULL
    ) STRICT""",
)

# Each statement is executed in the owning transaction, never executescript()
# (which could commit a caller's transaction).
RECOVERY_STATEMENTS = (
    """CREATE TABLE capture_inbox (
        token TEXT PRIMARY KEY NOT NULL, scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id),
        project_id TEXT, branch_id TEXT, created_at TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND length(payload_json)<=2097152),
        last_error_code TEXT
    ) STRICT""",
    """CREATE TABLE work_error_details (
        detail_id INTEGER PRIMARY KEY, work_id INTEGER NOT NULL REFERENCES work_items(work_id) ON DELETE CASCADE,
        lease_token INTEGER NOT NULL, stage TEXT NOT NULL, error_code TEXT NOT NULL,
        error_field TEXT, recorded_at TEXT NOT NULL
    ) STRICT""",
    "CREATE INDEX work_errors ON work_error_details(work_id,detail_id)",
    """CREATE TABLE consolidation_fragments (
        work_id INTEGER NOT NULL REFERENCES work_items(work_id) ON DELETE CASCADE,
        start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, total INTEGER NOT NULL,
        proposals_json TEXT NOT NULL CHECK(json_valid(proposals_json) AND length(proposals_json)<=131072),
        PRIMARY KEY(work_id,start_offset)
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE consolidation_outcomes (
        work_id INTEGER PRIMARY KEY REFERENCES work_items(work_id) ON DELETE CASCADE,
        disposition TEXT NOT NULL, detail TEXT NOT NULL, recorded_at TEXT NOT NULL
    ) STRICT""",
)

STATEMENTS = (
    """CREATE TABLE instance_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        agent_id TEXT NOT NULL, installation_id TEXT NOT NULL,
        data_directory TEXT NOT NULL, schema_version INTEGER NOT NULL,
        memory_epoch INTEGER NOT NULL DEFAULT 0 CHECK(memory_epoch>=0),
        config_version INTEGER NOT NULL DEFAULT 1 CHECK(config_version>=1),
        test_mode INTEGER NOT NULL CHECK(test_mode IN (0,1)),
        installation_kind TEXT NOT NULL DEFAULT 'local' CHECK(installation_kind IN ('local','shared'))
    ) STRICT""",
    """CREATE TABLE instance_scopes (scope_id TEXT PRIMARY KEY NOT NULL) STRICT""",
    """CREATE TABLE source_events (
        event_id TEXT NOT NULL, source_event_key TEXT NOT NULL,
        source_revision INTEGER NOT NULL CHECK(source_revision>=1),
        source_group_key TEXT NOT NULL,
        segment_index INTEGER NOT NULL DEFAULT 0 CHECK(segment_index>=0),
        segment_total INTEGER CHECK(segment_total IS NULL OR segment_total>segment_index),
        scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id),
        session_id TEXT NOT NULL, project_id TEXT, branch_id TEXT,
        origin TEXT NOT NULL CHECK(origin IN ('human_direct','assistant_visible',
            'tool_observation','external_document','host_generated','memory_reinjection','imported','origin_unknown')),
        role TEXT NOT NULL CHECK(role IN ('user','assistant','tool','system','document','unknown')),
        content TEXT NOT NULL CHECK(length(content)<=65536),
        content_sha256 TEXT NOT NULL, event_sha256 TEXT NOT NULL,
        occurred_at TEXT, recorded_at TEXT NOT NULL, persisted_at TEXT NOT NULL,
        time_precision TEXT NOT NULL CHECK(time_precision IN ('instant','day','approximate','unknown')),
        capture_state TEXT NOT NULL CHECK(capture_state IN ('complete','partial','gap')),
        source_original_origin TEXT, dataset_id TEXT, import_provenance_sha256 TEXT,
        extra_json TEXT NOT NULL CHECK(length(extra_json)<=131072 AND json_valid(extra_json)),
        capture_gaps_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(capture_gaps_json)),
        read_blocked INTEGER NOT NULL DEFAULT 0 CHECK(read_blocked IN (0,1)),
        suppressed INTEGER NOT NULL DEFAULT 0 CHECK(suppressed IN (0,1)),
        source_id INTEGER,
        entry_id TEXT NOT NULL DEFAULT 'local',
        PRIMARY KEY(event_id,source_revision),
        UNIQUE(source_event_key,source_revision),
        UNIQUE(source_group_key,source_revision,segment_index)
    ) STRICT""",
    "CREATE INDEX source_scope_time ON source_events(scope_id,occurred_at,event_id,source_revision)",
    # A repeated tool output is found by its content hash at capture (core/admission.py)
    # and by the retention pass (runtime/vector_retention.py).
    "CREATE INDEX source_content ON source_events(scope_id,role,content_sha256)",
    # A source version's integer identity, what the lexical postings name it by.
    "CREATE UNIQUE INDEX source_ids ON source_events(source_id)",
    WORK_ITEMS_STATEMENT,
    "CREATE INDEX work_ready ON work_items(state,available_at,work_id)",
    *LEXICAL_STATEMENTS,
    """CREATE TABLE claims (
        claim_id TEXT PRIMARY KEY NOT NULL,
        scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id),
        project_id TEXT, branch_id TEXT,
        subject TEXT NOT NULL, predicate TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('fact','preference','constraint','decision','procedure','intention','alias')),
        slot_key TEXT NOT NULL UNIQUE, current_revision INTEGER NOT NULL CHECK(current_revision>=1),
        read_blocked INTEGER NOT NULL DEFAULT 0 CHECK(read_blocked IN (0,1)),
        suppressed INTEGER NOT NULL DEFAULT 0 CHECK(suppressed IN (0,1)),
        FOREIGN KEY(claim_id,current_revision) REFERENCES claim_versions(claim_id,revision) DEFERRABLE INITIALLY DEFERRED
    ) STRICT""",
    "CREATE INDEX claim_scope_subject ON claims(scope_id,subject,predicate,project_id,branch_id)",
    """CREATE TABLE claim_versions (
        claim_id TEXT NOT NULL REFERENCES claims(claim_id), revision INTEGER NOT NULL CHECK(revision>=1),
        payload_json TEXT NOT NULL CHECK(length(payload_json)<=131072 AND json_valid(payload_json)),
        state TEXT NOT NULL CHECK(state IN ('proposed','active','disputed','superseded','retracted')),
        basis TEXT NOT NULL CHECK(basis IN ('direct_report','observed','derived_summary','inferred_suggestion','unknown')),
        qualification_reason TEXT NOT NULL,
        valid_from TEXT, valid_to TEXT, recorded_from TEXT NOT NULL, recorded_to TEXT,
        replaces_revision INTEGER, conflict_revisions_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(conflict_revisions_json)),
        PRIMARY KEY(claim_id,revision),
        FOREIGN KEY(claim_id,replaces_revision) REFERENCES claim_versions(claim_id,revision),
        CHECK(valid_from IS NULL OR valid_to IS NULL OR valid_from<valid_to),
        CHECK(recorded_to IS NULL OR recorded_from<=recorded_to)
    ) STRICT""",
    """CREATE TABLE evidence_links (
        object_kind TEXT NOT NULL CHECK(object_kind IN ('event','claim','episode','artifact','reference')),
        object_ref TEXT NOT NULL, object_revision INTEGER NOT NULL CHECK(object_revision>=1),
        source_ref TEXT NOT NULL, source_revision INTEGER NOT NULL CHECK(source_revision>=1),
        relation TEXT NOT NULL CHECK(relation IN ('supports','derived_from','contradicts')),
        quote TEXT NOT NULL CHECK(length(quote)<=4096), location TEXT,
        PRIMARY KEY(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote),
        FOREIGN KEY(source_ref,source_revision) REFERENCES source_events(event_id,source_revision)
    ) STRICT, WITHOUT ROWID""",
    "CREATE INDEX evidence_dependents ON evidence_links(source_ref,source_revision,object_kind,object_ref,object_revision)",
    """CREATE TABLE unresolved_updates (
        update_id TEXT PRIMARY KEY NOT NULL,
        source_ref TEXT NOT NULL, source_revision INTEGER NOT NULL,
        scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id), project_id TEXT, branch_id TEXT,
        candidate_refs_json TEXT NOT NULL CHECK(length(candidate_refs_json)<=16384 AND json_valid(candidate_refs_json)),
        state TEXT NOT NULL DEFAULT 'unresolved' CHECK(state IN ('unresolved','resolved','obsolete')),
        created_at TEXT NOT NULL, resolved_at TEXT,
        FOREIGN KEY(source_ref,source_revision) REFERENCES source_events(event_id,source_revision)
    ) STRICT""",
    """CREATE TABLE deletion_operations (
        operation_id TEXT PRIMARY KEY NOT NULL, request_sha256 TEXT NOT NULL UNIQUE,
        mode TEXT NOT NULL CHECK(mode IN ('suppress','delete')),
        scope_ids_json TEXT NOT NULL CHECK(json_valid(scope_ids_json)), project_id TEXT, branch_id TEXT,
        requested_refs_json TEXT NOT NULL CHECK(json_valid(requested_refs_json)),
        expected_revisions_json TEXT NOT NULL CHECK(json_valid(expected_revisions_json)),
        created_at TEXT NOT NULL, memory_epoch INTEGER NOT NULL,
        layers_json TEXT NOT NULL CHECK(json_valid(layers_json)),
        active_content_removed INTEGER NOT NULL DEFAULT 0 CHECK(active_content_removed IN (0,1))
    ) STRICT""",
    """CREATE TABLE object_blocks (
        object_kind TEXT NOT NULL CHECK(object_kind IN ('event','claim','episode','artifact','reference')),
        object_ref TEXT NOT NULL, scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id),
        project_id TEXT, branch_id TEXT,
        read_blocked INTEGER NOT NULL CHECK(read_blocked IN (0,1)),
        suppressed INTEGER NOT NULL CHECK(suppressed IN (0,1)),
        operation_id TEXT NOT NULL REFERENCES deletion_operations(operation_id),
        PRIMARY KEY(object_kind,object_ref)
    ) STRICT, WITHOUT ROWID""",
    "CREATE INDEX blocks_operation ON object_blocks(operation_id,object_kind,object_ref)",
    """CREATE TABLE source_group_blocks (
        group_sha256 TEXT PRIMARY KEY NOT NULL,
        scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id), project_id TEXT, branch_id TEXT,
        read_blocked INTEGER NOT NULL CHECK(read_blocked IN (0,1)),
        suppressed INTEGER NOT NULL CHECK(suppressed IN (0,1)),
        operation_id TEXT NOT NULL REFERENCES deletion_operations(operation_id)
    ) STRICT""",
    """CREATE TABLE deletion_members (
        operation_id TEXT NOT NULL REFERENCES deletion_operations(operation_id),
        object_kind TEXT NOT NULL, object_ref TEXT NOT NULL,
        PRIMARY KEY(operation_id,object_kind,object_ref)
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE restored_absence_blocks (
        object_kind TEXT NOT NULL CHECK(object_kind='claim'),object_ref TEXT NOT NULL,
        scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id),project_id TEXT,branch_id TEXT,
        checkpoint_sha256 TEXT NOT NULL,reason TEXT NOT NULL,
        PRIMARY KEY(object_kind,object_ref)
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE episodes (
        episode_id TEXT PRIMARY KEY NOT NULL,
        scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id),project_id TEXT,branch_id TEXT,
        anchor_key TEXT NOT NULL UNIQUE,anchor_kind TEXT NOT NULL CHECK(anchor_kind IN ('task','artifact','session')),
        series_key TEXT NOT NULL,segment_index INTEGER NOT NULL DEFAULT 0 CHECK(segment_index>=0),
        current_revision INTEGER NOT NULL DEFAULT 1 CHECK(current_revision>=1),
        read_blocked INTEGER NOT NULL DEFAULT 0 CHECK(read_blocked IN (0,1)),
        suppressed INTEGER NOT NULL DEFAULT 0 CHECK(suppressed IN (0,1))
    ) STRICT""",
    "CREATE INDEX episodes_context ON episodes(scope_id,project_id,branch_id)",
    "CREATE UNIQUE INDEX episode_series_segments ON episodes(series_key,segment_index)",
    """CREATE TABLE episode_versions (
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id),revision INTEGER NOT NULL CHECK(revision>=1),
        state TEXT NOT NULL CHECK(state IN ('open','completed','failed','cancelled','interrupted','unknown')),
        resume_json TEXT CHECK(resume_json IS NULL OR (json_valid(resume_json) AND length(resume_json)<=131072)),
        source_watermark TEXT NOT NULL,processed_sequence INTEGER NOT NULL DEFAULT 0,
        recorded_at TEXT NOT NULL,environment_revision TEXT,
        PRIMARY KEY(episode_id,revision)
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE episode_events (
        sequence INTEGER PRIMARY KEY,
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id),source_ref TEXT NOT NULL,source_revision INTEGER NOT NULL,
        membership TEXT NOT NULL CHECK(membership IN ('anchored','provisional')),environment_revision TEXT,
        UNIQUE(source_ref,source_revision),
        FOREIGN KEY(source_ref,source_revision) REFERENCES source_events(event_id,source_revision)
    ) STRICT""",
    "CREATE INDEX episode_sequence ON episode_events(episode_id,sequence)",
    """CREATE TABLE artifacts (
        artifact_id TEXT PRIMARY KEY NOT NULL,
        scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id),project_id TEXT,branch_id TEXT,
        current_revision INTEGER NOT NULL CHECK(current_revision>=1),
        read_blocked INTEGER NOT NULL DEFAULT 0 CHECK(read_blocked IN (0,1)),
        suppressed INTEGER NOT NULL DEFAULT 0 CHECK(suppressed IN (0,1))
    ) STRICT""",
    """CREATE TABLE artifact_versions (
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),revision INTEGER NOT NULL CHECK(revision>=1),
        label TEXT NOT NULL,media_type TEXT NOT NULL,sha256 TEXT,size_bytes INTEGER,
        retention_state TEXT NOT NULL CHECK(retention_state IN ('reference_only','retained_artifact','described_artifact')),
        relative_path TEXT,blob_json TEXT CHECK(blob_json IS NULL OR json_valid(blob_json)),description_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(description_json)),
        recorded_at TEXT NOT NULL,PRIMARY KEY(artifact_id,revision)
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE reference_bindings (
        reference_id TEXT PRIMARY KEY NOT NULL,
        scope_id TEXT NOT NULL REFERENCES instance_scopes(scope_id),project_id TEXT,branch_id TEXT,
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id),current_revision INTEGER NOT NULL CHECK(current_revision>=1),
        read_blocked INTEGER NOT NULL DEFAULT 0 CHECK(read_blocked IN (0,1)),
        suppressed INTEGER NOT NULL DEFAULT 0 CHECK(suppressed IN (0,1))
    ) STRICT""",
    """CREATE TABLE reference_versions (
        reference_id TEXT NOT NULL REFERENCES reference_bindings(reference_id),revision INTEGER NOT NULL CHECK(revision>=1),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND length(payload_json)<=32768),
        qualification_reason TEXT NOT NULL,recorded_at TEXT NOT NULL,
        PRIMARY KEY(reference_id,revision)
    ) STRICT, WITHOUT ROWID""",
    """CREATE TABLE object_dependencies (
        object_kind TEXT NOT NULL,object_ref TEXT NOT NULL,object_revision INTEGER NOT NULL,
        dependency_kind TEXT NOT NULL,dependency_ref TEXT NOT NULL,dependency_revision INTEGER NOT NULL,
        PRIMARY KEY(object_kind,object_ref,object_revision,dependency_kind,dependency_ref,dependency_revision)
    ) STRICT, WITHOUT ROWID""",
    "CREATE INDEX object_dependents ON object_dependencies(dependency_kind,dependency_ref,dependency_revision)",
    EXPIRED_VECTORS_STATEMENT,
    *AUTHORIZATION_STATEMENTS,
    ENTRIES_STATEMENT,
) + RECOVERY_STATEMENTS + CANDIDATE_STATEMENTS


def upgrade_1105(connection):
    """Add a content-free consolidation checkpoint inside explicit initialization.

    The caller must verify the old database's binding and own its write
    transaction before calling. Normal reads never migrate a database.
    """
    connection.execute("ALTER TABLE work_items ADD COLUMN consolidation_offset INTEGER NOT NULL DEFAULT 0 CHECK(consolidation_offset>=0)")
    connection.execute("UPDATE instance_meta SET schema_version=1106 WHERE singleton=1")
    connection.execute("PRAGMA user_version=1106")


def upgrade_1106(connection):
    """Install bounded durable recovery inside the verified initialization transaction."""
    for statement in RECOVERY_STATEMENTS:
        connection.execute(statement)
    # A new decoder gets one version-bound opportunity. Preserve old failures
    # before requeuing; never reset them on ordinary startup or retry loops.
    connection.execute("""INSERT INTO work_error_details(work_id,lease_token,stage,error_code,error_field,recorded_at)
        SELECT work_id,lease_token,'upgrade_1106',last_error_code,NULL,available_at FROM work_items
        WHERE work_type='consolidate' AND state='failed' AND lower(last_error_code) LIKE '%derivation_invalid'""")
    connection.execute("""UPDATE work_items SET state='pending',attempt=0,consolidation_offset=0,
        lease_token=lease_token+1,lease_owner=NULL,lease_until=NULL,last_error_code='decoder_upgrade:1107'
        WHERE work_type='consolidate' AND state='failed' AND lower(last_error_code) LIKE '%derivation_invalid'""")
    # Old offsets have no fragment summaries. Replay their already durable
    # source from zero to establish verifiable complete coverage.
    connection.execute("""UPDATE work_items SET state='pending',consolidation_offset=0,
        lease_token=lease_token+1,lease_owner=NULL,lease_until=NULL
        WHERE work_type='consolidate' AND consolidation_offset>0 AND state IN ('pending','leased')""")
    connection.execute("UPDATE instance_meta SET schema_version=1107 WHERE singleton=1")
    connection.execute("PRAGMA user_version=1107")


def upgrade_1107(connection):
    """Add candidate lifecycle storage without losing any durable work history.

    SQLite cannot widen a CHECK constraint in place.  The child rows are
    staged inside the caller's migration transaction, then recreated against
    the widened parent.  Work IDs, leases, attempts, errors and consolidation
    checkpoints are copied byte-for-byte.
    """
    connection.execute("CREATE TEMP TABLE _r1_work_errors AS SELECT * FROM work_error_details")
    connection.execute("CREATE TEMP TABLE _r1_fragments AS SELECT * FROM consolidation_fragments")
    connection.execute("CREATE TEMP TABLE _r1_outcomes AS SELECT * FROM consolidation_outcomes")
    connection.execute("DROP TABLE consolidation_outcomes")
    connection.execute("DROP TABLE consolidation_fragments")
    connection.execute("DROP TABLE work_error_details")
    connection.execute(WORK_ITEMS_STATEMENT.replace("work_items (", "work_items_1108 (", 1))
    connection.execute("""INSERT INTO work_items_1108(
        work_id,work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,state,attempt,
        available_at,lease_token,lease_owner,lease_until,last_error_code,consolidation_offset)
        SELECT work_id,work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,state,attempt,
        available_at,lease_token,lease_owner,lease_until,last_error_code,consolidation_offset FROM work_items""")
    connection.execute("DROP TABLE work_items")
    connection.execute("ALTER TABLE work_items_1108 RENAME TO work_items")
    connection.execute("CREATE INDEX work_ready ON work_items(state,available_at,work_id)")
    for statement in RECOVERY_STATEMENTS[1:]:
        connection.execute(statement)
    connection.execute("""INSERT INTO work_error_details(
        detail_id,work_id,lease_token,stage,error_code,error_field,recorded_at)
        SELECT detail_id,work_id,lease_token,stage,error_code,error_field,recorded_at FROM _r1_work_errors""")
    connection.execute("""INSERT INTO consolidation_fragments(
        work_id,start_offset,end_offset,total,proposals_json)
        SELECT work_id,start_offset,end_offset,total,proposals_json FROM _r1_fragments""")
    connection.execute("""INSERT INTO consolidation_outcomes(work_id,disposition,detail,recorded_at)
        SELECT work_id,disposition,detail,recorded_at FROM _r1_outcomes""")
    connection.execute("DROP TABLE _r1_work_errors")
    connection.execute("DROP TABLE _r1_fragments")
    connection.execute("DROP TABLE _r1_outcomes")
    for statement in CANDIDATE_STATEMENTS:
        connection.execute(statement)
    connection.execute("UPDATE instance_meta SET schema_version=1108 WHERE singleton=1")
    connection.execute("PRAGMA user_version=1108")


def upgrade_1108(connection):
    """Keep each episode lineage row once, at the revision its source entered.

    Every attach copied the previous revision's evidence links and object
    dependencies onto the new revision, so a 200-event segment held 20,100
    link rows for 200 sources.  Readers now take every row at or below the
    revision they ask for, which makes the earliest copy the one to keep.

    The same step adds the retention ledger, ``expired_vectors``, the content
    index a repeated tool output is found by, moves the migrated scope
    authorizations out of every source row (8.5 s for 167,000 sources on a
    1.4 GB store), numbers every source version and rebuilds the lexical
    index as a term dictionary with integer postings (95 s for 5.2 million
    rows on that store, 713 MB down to 135 MB).
    """
    # A row goes when an earlier copy of it exists.  The probe runs on the
    # evidence_dependents index: four seconds for 180,000 rows on a 1.4 GB store,
    # where a row-value NOT IN over the grouped minimum took eight minutes.
    connection.execute("""DELETE FROM evidence_links WHERE object_kind='episode' AND EXISTS (
        SELECT 1 FROM evidence_links k WHERE k.source_ref=evidence_links.source_ref AND k.source_revision=evidence_links.source_revision
        AND k.object_kind='episode' AND k.object_ref=evidence_links.object_ref AND k.object_revision<evidence_links.object_revision
        AND k.relation=evidence_links.relation AND k.quote=evidence_links.quote)""")
    connection.execute("""DELETE FROM object_dependencies WHERE object_kind='episode' AND EXISTS (
        SELECT 1 FROM object_dependencies k WHERE k.object_kind='episode' AND k.object_ref=object_dependencies.object_ref
        AND k.dependency_kind=object_dependencies.dependency_kind AND k.dependency_ref=object_dependencies.dependency_ref
        AND k.dependency_revision=object_dependencies.dependency_revision AND k.object_revision<object_dependencies.object_revision)""")
    connection.execute(EXPIRED_VECTORS_STATEMENT)
    connection.execute("CREATE INDEX source_content ON source_events(scope_id,role,content_sha256)")
    for statement in AUTHORIZATION_STATEMENTS:
        connection.execute(statement)
    normalize_scope_authorizations(connection)
    connection.execute("ALTER TABLE source_events ADD COLUMN source_id INTEGER")
    connection.execute("UPDATE source_events SET source_id=rowid")
    connection.execute("CREATE UNIQUE INDEX source_ids ON source_events(source_id)")
    for statement in LEXICAL_STATEMENTS:
        connection.execute(statement)
    connection.execute("INSERT INTO lexical_terms(term) SELECT DISTINCT term FROM lexical_projection ORDER BY term")
    connection.execute("""INSERT OR IGNORE INTO lexical_postings(term_id,source_id)
        SELECT t.term_id,e.source_id FROM lexical_projection p
        JOIN lexical_terms t ON t.term=p.term
        JOIN source_events e ON e.event_id=p.event_id AND e.source_revision=p.source_revision""")
    connection.execute("DROP TABLE lexical_projection")
    connection.execute("UPDATE instance_meta SET schema_version=1109 WHERE singleton=1")
    connection.execute("PRAGMA user_version=1109")


def upgrade_1109(connection):
    """Name the kind of store this is, and the entry each source came in through.

    Every row of a store that predates entries came in through its own host, so
    both new columns take a constant default and SQLite adds them without
    rewriting a row: the step costs the same on a 1.5 GB store as on an empty
    one.  ``entries`` starts empty; a shared store fills it as entries attach.
    """
    connection.execute("""ALTER TABLE instance_meta ADD COLUMN installation_kind TEXT NOT NULL DEFAULT 'local'
        CHECK(installation_kind IN ('local','shared'))""")
    connection.execute("ALTER TABLE source_events ADD COLUMN entry_id TEXT NOT NULL DEFAULT 'local'")
    connection.execute(ENTRIES_STATEMENT)
    connection.execute("UPDATE instance_meta SET schema_version=1110 WHERE singleton=1")
    connection.execute("PRAGMA user_version=1110")


def normalize_scope_authorizations(connection) -> int:
    """Move each source's ``scope_authorization`` out of its extra_json, kept once per distinct payload.

    Returns the number of sources rewritten.  Safe to repeat: a source already
    moved has nothing left to move.
    """
    connection.execute("""INSERT OR IGNORE INTO authorization_payloads(payload)
        SELECT DISTINCT json_extract(extra_json,'$.scope_authorization') FROM source_events
        WHERE json_type(extra_json,'$.scope_authorization') IS NOT NULL""")
    connection.execute("""INSERT OR IGNORE INTO source_authorizations(event_id,source_revision,authorization_id)
        SELECT e.event_id,e.source_revision,p.authorization_id FROM source_events e
        JOIN authorization_payloads p ON p.payload=json_extract(e.extra_json,'$.scope_authorization')
        WHERE json_type(e.extra_json,'$.scope_authorization') IS NOT NULL""")
    return connection.execute("""UPDATE source_events SET extra_json=json_remove(extra_json,'$.scope_authorization')
        WHERE json_type(extra_json,'$.scope_authorization') IS NOT NULL""").rowcount
