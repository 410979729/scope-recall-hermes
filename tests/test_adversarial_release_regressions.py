"""Adversarial release regressions for persistence, governance, and concurrency boundaries."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plugins.memory import load_memory_provider
from scope_recall.candidate_promotion import classify_candidate_row
from scope_recall.capture_filters import sanitize_structured_value
from scope_recall.freshness import upsert_memory_freshness
from scope_recall.journal import append_journal_entry
from scope_recall.models import RuntimeScope
from scope_recall.scope import build_scope_id, build_shared_scope_id
from scope_recall.sql_store import ensure_schema, record_governance_audit_event, store_row

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "promote.memory_candidates.py"


def _disable_unrelated_vector_runtime(hermes_home: Path) -> None:
    """Keep metadata-boundary tests independent of optional native runtimes."""

    config_path = hermes_home / "scope-recall" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"vector": {"enabled": False}}) + "\n",
        encoding="utf-8",
    )


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _insert_candidate(
    conn: sqlite3.Connection,
    memory_id: str,
    *,
    content: str = 'Run pytest and doctor before rollout with stable evidence.',
    summary: str = 'Stable workflow',
    memory_type: str = 'workflow',
    candidate_status: str = 'needs_review',
) -> None:
    at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    metadata = {
        'lifecycle': 'candidate',
        'candidate_status': candidate_status,
        'memory_type': memory_type,
        'confidence': 0.9,
        'importance': 0.8,
        'evidence_refs': ['journal:synthetic'],
    }
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, platform, user_id, chat_id, thread_id,
            gateway_session_key, agent_identity, agent_workspace, session_id,
            source, target, content, summary, created_at, updated_at,
            last_recalled_turn, metadata
        ) VALUES (?, 'scope-a', 'audit', 'synthetic', '', '', '', 'yuheng',
                  'hermes', 'audit', 'journal-digest', 'ops', ?, ?, ?, ?, 0, ?)
        """,
        (memory_id, content, summary, at, at, json.dumps(metadata, sort_keys=True)),
    )
    conn.commit()


def test_bulk_promotion_keeps_candidate_status_in_sync(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    db_dir = home / 'scope-recall'
    db_dir.mkdir(parents=True)
    conn = _open(db_dir / 'memory.sqlite3')
    try:
        _insert_candidate(conn, 'candidate-promote')
    finally:
        conn.close()

    script = _load_script('audit_promote_status')
    result = script.promote_memory_candidates(
        home,
        apply=True,
        scope_ids=['scope-a'],
        review_ids=['candidate-promote'],
        review_decision='promote',
        review_reason='synthetic reviewed candidate',
    )
    assert result['ok'] is True

    conn = sqlite3.connect(db_dir / 'memory.sqlite3')
    try:
        lifecycle, status = conn.execute(
            "SELECT json_extract(metadata, '$.lifecycle'), json_extract(metadata, '$.candidate_status') FROM memories WHERE id='candidate-promote'"
        ).fetchone()
    finally:
        conn.close()
    assert lifecycle == 'promoted'
    assert status == 'promoted'


def test_bulk_archive_cleans_hidden_graph_companions(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    db_dir = home / 'scope-recall'
    db_dir.mkdir(parents=True)
    conn = _open(db_dir / 'memory.sqlite3')
    try:
        _insert_candidate(conn, 'candidate-archive')
        _insert_candidate(conn, 'peer')
        conn.execute(
            "INSERT INTO memory_entities(memory_id, entity, weight, source) VALUES ('candidate-archive', 'Synthetic', 1.0, 'synthetic')"
        )
        conn.execute(
            "INSERT INTO memory_relations(source_memory_id, target_memory_id, relation_type, confidence, note, created_at) VALUES ('candidate-archive', 'peer', 'same_topic', 0.8, 'synthetic', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()

    script = _load_script('audit_archive_companion')
    result = script.promote_memory_candidates(
        home,
        apply=True,
        scope_ids=['scope-a'],
        review_ids=['candidate-archive'],
        review_decision='archive',
        review_reason='synthetic reviewed candidate',
    )
    assert result['ok'] is True

    conn = sqlite3.connect(db_dir / 'memory.sqlite3')
    try:
        entity_count = conn.execute("SELECT COUNT(*) FROM memory_entities WHERE memory_id='candidate-archive'").fetchone()[0]
        relation_count = conn.execute("SELECT COUNT(*) FROM memory_relations WHERE source_memory_id='candidate-archive' OR target_memory_id='candidate-archive'").fetchone()[0]
    finally:
        conn.close()
    assert entity_count == 0
    assert relation_count == 0


def test_candidate_conflict_query_error_fails_closed() -> None:
    conn = sqlite3.connect(':memory:')
    row = {
        'id': 'candidate',
        'scope_id': 'scope-a',
        'target': 'ops',
        'source': 'journal-digest',
        'summary': 'Stable workflow',
        'content': 'Run pytest and doctor before rollout with stable evidence.',
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'metadata': json.dumps({
            'lifecycle': 'candidate',
            'memory_type': 'workflow',
            'confidence': 0.9,
            'importance': 0.8,
            'evidence_refs': ['journal:synthetic'],
        }),
    }
    try:
        decision = classify_candidate_row(row, conn)
    finally:
        conn.close()
    assert decision.action != 'promote', 'a failed conflict query must not silently authorize promotion'


def test_bulk_promotion_does_not_overwrite_concurrent_metadata(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    db_dir = home / 'scope-recall'
    db_dir.mkdir(parents=True)
    conn = _open(db_dir / 'memory.sqlite3')
    try:
        _insert_candidate(conn, 'candidate-race')
    finally:
        conn.close()

    script = _load_script('audit_concurrent_metadata')
    original = script.classify_candidate_row
    reached = threading.Event()
    resume = threading.Event()

    def paused_classifier(row, conn):
        reached.set()
        assert resume.wait(5)
        return original(row, conn)

    script.classify_candidate_row = paused_classifier
    outcome: dict[str, object] = {}

    def run_apply() -> None:
        outcome['result'] = script.promote_memory_candidates(
            home,
            apply=True,
            scope_ids=['scope-a'],
            review_ids=['candidate-race'],
            review_decision='promote',
            review_reason='synthetic reviewed candidate',
        )

    worker = threading.Thread(target=run_apply)
    worker.start()
    assert reached.wait(5)
    peer = sqlite3.connect(db_dir / 'memory.sqlite3')
    try:
        raw = peer.execute("SELECT metadata FROM memories WHERE id='candidate-race'").fetchone()[0]
        metadata = json.loads(raw)
        metadata['concurrent_marker'] = 'must-survive'
        peer.execute(
            "UPDATE memories SET metadata=?, updated_at=? WHERE id='candidate-race'",
            (json.dumps(metadata, sort_keys=True), datetime.now(timezone.utc).isoformat()),
        )
        peer.commit()
    finally:
        peer.close()
    resume.set()
    worker.join(10)
    assert not worker.is_alive()

    conn = sqlite3.connect(db_dir / 'memory.sqlite3')
    try:
        metadata = json.loads(conn.execute("SELECT metadata FROM memories WHERE id='candidate-race'").fetchone()[0])
    finally:
        conn.close()
    assert metadata.get('concurrent_marker') == 'must-survive'


def test_folded_data_url_redaction_preserves_surrounding_prose() -> None:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    scope = RuntimeScope(
        platform='telegram',
        user_id='synthetic',
        chat_id='dm',
        agent_identity='yuheng',
        agent_workspace='hermes',
    )
    continuation = 'B' * 512
    entry_id = append_journal_entry(
        conn,
        scope=scope,
        scope_id=build_scope_id(scope),
        shared_scope_id=build_shared_scope_id(scope),
        session_id='synthetic-folded-data-url',
        turn_number=1,
        role='user',
        content='before data:image/png;base64,' + ('A' * 512) + '\n' + continuation + ' after',
    )
    assert entry_id > 0
    content = conn.execute('SELECT content FROM journal_entries WHERE id=?', (entry_id,)).fetchone()[0]
    conn.close()
    assert 'before' in content and 'after' in content
    assert continuation not in content


def test_structured_redaction_preserves_benign_token_telemetry_keys() -> None:
    sanitized, changed = sanitize_structured_value({'token_count': 42})

    assert sanitized == {'token_count': 42}
    assert changed is False


def test_governance_redaction_sanitizes_mapping_keys() -> None:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    marker = 'Z' * 24
    secret_key = 'api_key=' + marker
    record_governance_audit_event(
        conn,
        event_id='synthetic-key-redaction',
        event_type='synthetic',
        action='audit',
        before={secret_key: True},
    )
    rendered = conn.execute("SELECT before_json FROM governance_audit_events WHERE id='synthetic-key-redaction'").fetchone()[0]
    conn.close()
    assert marker not in rendered


def test_public_store_scrubs_data_url_before_sqlite_truth(tmp_path: Path) -> None:
    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider('scope-recall')
    assert plugin is not None
    plugin.initialize(
        'synthetic-data-url-store',
        hermes_home=str(tmp_path),
        platform='telegram',
        user_id='synthetic',
        chat_id='dm',
        agent_context='primary',
        agent_identity='yuheng',
        agent_workspace='hermes',
    )
    first_payload = 'A' * 512
    continuation = 'B' * 512
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                'scope_recall_store',
                {
                    'content': (
                        'tool before data:image/png;base64,'
                        + first_payload
                        + '\n'
                        + continuation
                        + '. tool after'
                    ),
                    'target': 'ops',
                    'memory_type': 'procedure',
                },
            )
        )
        assert stored['stored'] is True
    finally:
        plugin.shutdown()

    conn = sqlite3.connect(tmp_path / 'scope-recall' / 'memory.sqlite3')
    try:
        persisted = str(conn.execute('SELECT content FROM memories WHERE id = ?', (stored['id'],)).fetchone()[0])
    finally:
        conn.close()
    assert 'tool before' in persisted and '. tool after' in persisted
    assert 'data:image' not in persisted
    assert first_payload[:128] not in persisted
    assert continuation not in persisted


def test_public_store_and_compact_inspect_do_not_round_trip_secret_like_metadata_keys(tmp_path: Path) -> None:
    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider('scope-recall')
    assert plugin is not None
    plugin.initialize(
        'synthetic-metadata-key',
        hermes_home=str(tmp_path),
        platform='telegram',
        user_id='synthetic',
        chat_id='dm',
        agent_context='primary',
        agent_identity='yuheng',
        agent_workspace='hermes',
    )
    marker = 'Q' * 24
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                'scope_recall_store',
                {
                    'content': 'Synthetic factual endpoint requires a manual live check.',
                    'target': 'ops',
                    'memory_type': 'factual',
                    'freshness': {
                        'status': 'needs_live_check',
                        'validator_kind': 'manual',
                        'validator_spec': {'api_key=' + marker: True},
                    },
                },
            )
        )
        assert stored['stored'] is True
        inspected = plugin.handle_tool_call(
            'scope_recall_memory',
            {'action': 'inspect', 'id': stored['id']},
        )
    finally:
        plugin.shutdown()
    assert marker not in inspected


def test_public_store_does_not_persist_secret_like_validator_keys(tmp_path: Path) -> None:
    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider('scope-recall')
    assert plugin is not None
    plugin.initialize(
        'synthetic-persisted-key',
        hermes_home=str(tmp_path),
        platform='telegram',
        user_id='synthetic',
        chat_id='dm',
        agent_context='primary',
        agent_identity='yuheng',
        agent_workspace='hermes',
    )
    marker = 'W' * 24
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                'scope_recall_store',
                {
                    'content': 'Synthetic factual endpoint requires a manual live check.',
                    'target': 'ops',
                    'memory_type': 'factual',
                    'freshness': {
                        'status': 'needs_live_check',
                        'validator_kind': 'manual',
                        'validator_spec': {'api_key=' + marker: True},
                    },
                },
            )
        )
        assert stored['stored'] is True
    finally:
        plugin.shutdown()
    conn = sqlite3.connect(tmp_path / 'scope-recall' / 'memory.sqlite3')
    rendered = conn.execute(
        "SELECT validator_spec FROM fact_freshness WHERE subject_id=?",
        (stored['id'],),
    ).fetchone()[0]
    conn.close()
    assert marker not in rendered


def test_freshness_validator_spec_sanitizes_mapping_keys() -> None:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO memories(
            id, scope_id, source, target, content, summary, created_at, updated_at,
            platform, user_id, chat_id, thread_id, gateway_session_key,
            agent_identity, agent_workspace, session_id, last_recalled_turn, metadata
        ) VALUES ('fact', 'scope-a', 'tool-store', 'ops', 'Synthetic factual memory.',
                  'Synthetic factual memory.', ?, ?, 'audit', 'synthetic', '', '', '',
                  'yuheng', 'hermes', 'audit', 0, '{}')
        """,
        (now, now),
    )
    marker = 'Y' * 24
    upsert_memory_freshness(
        conn,
        memory_id='fact',
        metadata={
            'memory_type': 'factual',
            'freshness': {
                'status': 'needs_live_check',
                'validator_kind': 'manual',
                'validator_spec': {'api_key=' + marker: True},
            },
        },
    )
    rendered = conn.execute("SELECT validator_spec FROM fact_freshness WHERE subject_id='fact'").fetchone()[0]
    conn.close()
    assert marker not in rendered


def test_public_store_sanitizes_secret_like_tags_and_entities(tmp_path: Path) -> None:
    _disable_unrelated_vector_runtime(tmp_path)
    plugin = load_memory_provider('scope-recall')
    assert plugin is not None
    plugin.initialize(
        'synthetic-metadata-boundary',
        hermes_home=str(tmp_path),
        platform='telegram',
        user_id='synthetic',
        chat_id='dm',
        agent_context='primary',
        agent_identity='yuheng',
        agent_workspace='hermes',
    )
    marker = 'M' * 24
    secret = 'sk-' + marker
    private_path = '/home/synthetic/.ssh/id_ed25519'
    try:
        stored = json.loads(
            plugin.handle_tool_call(
                'scope_recall_store',
                {
                    'content': 'Synthetic durable preference uses concise operator reports.',
                    'target': 'user',
                    'tags': ['api_key=' + secret],
                    'entities': [private_path],
                },
            )
        )
        assert stored['stored'] is True
    finally:
        plugin.shutdown()

    conn = sqlite3.connect(tmp_path / 'scope-recall' / 'memory.sqlite3')
    rendered = conn.execute('SELECT metadata FROM memories WHERE id = ?', (stored['id'],)).fetchone()[0]
    conn.close()
    assert marker not in rendered
    assert secret not in rendered
    assert private_path not in rendered
    assert '[redacted_' in rendered.lower()


def test_store_row_sanitizes_unknown_metadata_keys_values_and_paths() -> None:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    marker = 'N' * 24
    private_path = '/home/synthetic/.ssh/id_rsa'
    store_row(
        conn,
        memory_id='metadata-boundary',
        scope_id='scope-a',
        platform='test',
        user_id='synthetic',
        chat_id='',
        thread_id='',
        gateway_session_key='',
        agent_identity='yuheng',
        agent_workspace='hermes',
        session_id='metadata-boundary',
        source='tool-store',
        target='ops',
        content='Synthetic operational fact requires safe structured metadata persistence.',
        metadata={
            'api_key=' + marker: True,
            'unknown_nested': {'token': 'sk-' + marker, 'private_path': private_path},
        },
    )
    rendered = conn.execute("SELECT metadata FROM memories WHERE id='metadata-boundary'").fetchone()[0]
    conn.close()
    assert marker not in rendered
    assert 'api_key=' not in rendered
    assert private_path not in rendered
    assert '[REDACTED_KEY]' in rendered
    assert '[redacted_' in rendered.lower()
