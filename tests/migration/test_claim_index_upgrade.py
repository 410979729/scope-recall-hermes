"""Resumable current-fact projection pages preserve SQLite truth and rollback material."""

import sqlite3
from contextlib import closing

import pytest
from scope_recall.core import lexical_index
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.runtime.store_maintenance import store_upgrade_pending

from tests.contract.test_claims import accept, app, capture, draft
from tests.v11_support import downgrade_store


def test_claim_index_upgrade_resumes_after_committed_page(app, monkeypatch):
    core, ctx = app
    refs = []
    for index in range(4):
        subject = f"TEST-upgrade-{index}"
        source = capture(core, ctx, f"{subject} theme amber.")
        refs.append(accept(core, ctx, draft(source, "amber", subject=subject, predicate="theme")).items[0].ref)
    truth = [core.current_claim(ctx, ref).payload for ref in refs]
    downgrade_store(core.storage.path, 1116)
    page = lexical_index.backfill_claim_page
    calls = []

    def interrupt(conn, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise InterruptedError("TEST interrupted before second page")
        return page(conn, limit=1)

    with monkeypatch.context() as patch:
        patch.setattr(lexical_index, "backfill_claim_page", interrupt)
        with pytest.raises(InterruptedError):
            core.storage.initialize()
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute(
            "SELECT processed_count,completed FROM candidate_scan_cursors WHERE cursor_name='claim_lexical'"
        ).fetchone() == (1, 0)
        assert conn.execute("SELECT count(DISTINCT claim_id) FROM claim_lexical_postings").fetchone()[0] == 1
    assert len(tuple(core.storage.path.parent.glob("upgrade-snapshots/*.sqlite3"))) == 1
    assert store_upgrade_pending(ctx.binding)
    core.storage.initialize()
    core.storage.initialize()
    assert not store_upgrade_pending(ctx.binding)
    with core.storage.read(ctx) as tx:
        assert not lexical_index.claim_backfill_pending(tx._check())
        assert tx._check().execute("SELECT count(DISTINCT claim_id) FROM claim_lexical_postings").fetchone()[0] == 4
        assert tx._check().execute("PRAGMA foreign_key_check").fetchall() == []
    assert [core.current_claim(ctx, ref).payload for ref in refs] == truth


def test_pending_claim_backfill_reports_the_schema_gap_until_upgrade_finishes(app, monkeypatch):
    """Foreground admission rejects pending upgrades; the pipeline reports empty results and a schema gap."""
    from scope_recall.contracts import ContractError
    from scope_recall.core import storage

    from tests.contract.test_claim_lexical_index import search
    from tests.v11_support import recall_request

    core, ctx = app
    source = capture(core, ctx, "TEST-upgrade theme amber.")
    accept(core, ctx, draft(source, "amber", subject="TEST-upgrade", predicate="theme"))
    with closing(sqlite3.connect(core.storage.path)) as conn:
        conn.execute(
            "INSERT INTO candidate_scan_cursors(cursor_name,updated_at) VALUES ('claim_lexical',datetime('now'))"
        )
        conn.commit()
    # A small synthetic fixture follows the admission branch of a large store;
    # its pending cursor and foreground packet are real, not mocked results.
    monkeypatch.setattr(storage, "HEAVY_UPGRADE_BYTES", 0)
    with pytest.raises(ContractError, match="SCHEMA_UNSUPPORTED"):
        core.recall_packet(ctx, recall_request(query="TEST-upgrade theme", mode="current"), deadline_seconds=1)
    result = core.recall_pipeline.search(search(core, ctx, "TEST-upgrade theme"))
    assert result.candidates == ()
    assert "sqlite_unavailable:SCHEMA_UNSUPPORTED" in result.gaps
    core.storage.initialize(unbounded_upgrade=True)
    with core.storage.read(ctx, remaining_seconds=60) as tx:
        from scope_recall.core.retrieval_storage import RetrievalStorage

        assert RetrievalStorage().claims(tx, search(core, ctx, "TEST-upgrade theme"), limit=6)
    recovered = core.recall_packet(ctx, recall_request(query="TEST-upgrade theme", mode="current"), deadline_seconds=1)
    assert "sqlite_unavailable:SCHEMA_UNSUPPORTED" not in recovered["gaps"]
    assert recovered["items"]


def test_subject_projection_upgrades_an_existing_body_index(app):
    """The preceding schema's complete body cursor must restart for subject markers."""
    from scope_recall.core.retrieval_storage import RetrievalStorage

    from tests.contract.test_claim_lexical_index import search

    core, ctx = app
    source = capture(core, ctx, "TEST-upgrade theme amber.")
    ref = accept(core, ctx, draft(source, "amber", subject="TEST-upgrade", predicate="theme")).items[0].ref
    downgrade_store(core.storage.path, 1117)
    core.storage.initialize()
    with core.storage.read(ctx) as tx:
        assert (
            tx._check()
            .execute(
                "SELECT subject_terms FROM claim_lexical_postings p JOIN lexical_terms t USING(term_id) "
                "WHERE p.claim_id=? AND t.term='test-upgrade'",
                (ref,),
            )
            .fetchone()[0]
            == 1
        )
        assert not lexical_index.claim_backfill_pending(tx._check())
        assert [
            item.ref for item in RetrievalStorage().claims(tx, search(core, ctx, "TEST-upgrade theme amber"), limit=6)
        ] == [ref]


@pytest.mark.parametrize("version", range(1105, SCHEMA_VERSION))
def test_each_published_format_preserves_truth_and_finishes_fact_nomination(app, monkeypatch, version):
    """Entity and screening tables coexist with both resumable fact projection steps."""
    from scope_recall.core.retrieval_storage import RetrievalStorage
    from scope_recall.core.storage import SCHEMA_MIGRATIONS
    from scope_recall.maintenance.shared_import import SOURCE_SCHEMAS

    from tests.contract.test_claim_lexical_index import _ideal_claims, _synthetic_claims, search

    core, ctx = app
    records = [
        ("claim-TEST-upgrade-a", "alpha beta", "cat", "amber"),
        ("claim-TEST-upgrade-b", "TEST-ocean", "deploy", "cobalt"),
    ]
    _synthetic_claims(core, ctx, records)
    with core.storage.read(ctx) as tx:
        original = _truth_hash(tx._check())
    downgrade_store(core.storage.path, version)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert ("entity_alias_proofs" in tables) == (version >= 1113)
        assert ("source_safety" in tables) == (version >= 1114)
        assert ("source_scan_progress" in tables) == (version >= 1116)
    core.storage.initialize(remaining_seconds=30)
    with core.storage.read(ctx) as tx:
        conn = tx._check()
        assert _truth_hash(conn) == original
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1118
        assert conn.execute("SELECT schema_version FROM instance_meta").fetchone()[0] == 1118
        assert not lexical_index.claim_backfill_pending(conn)
        assert conn.execute("SELECT count(DISTINCT claim_id) FROM claim_lexical_postings").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM injection_rules").fetchone()[0] >= 10
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for mode in ("current", "auto", "as_of", "history"):
            for query in ("alpha beta cat amber", "TEST-ocean deploy cobalt"):
                context = search(core, ctx, query, mode)
                actual = RetrievalStorage().claims(tx, context, limit=1)
                assert actual == _ideal_claims(tx, context, 1, monkeypatch)
                assert actual
    assert tuple(SCHEMA_MIGRATIONS) == tuple(range(1105, 1118))
    assert all(step.__name__ == f"upgrade_{v}" for v, step in SCHEMA_MIGRATIONS.items())
    assert SOURCE_SCHEMAS == frozenset(range(1109, 1119))


def _truth_hash(conn):
    """Hash original source bytes and fact payloads independently of derived projection state."""
    import hashlib
    import json

    values = [
        list(map(tuple, conn.execute(sql)))
        for sql in (
            "SELECT event_id,source_revision,content,content_sha256 FROM source_events "
            "ORDER BY event_id,source_revision",
            "SELECT claim_id,revision,payload_json FROM claim_versions ORDER BY claim_id,revision",
        )
    ]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()
