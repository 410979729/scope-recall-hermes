"""Database lifetimes survive every supported upgrade and content import."""

import sqlite3
from contextlib import closing

import pytest
from scope_recall.core.memory_import import ImportRecord, import_record
from scope_recall.core.schema import SCHEMA_VERSION

from tests.contract.test_claims import accept, app, capture, draft
from tests.v11_support import downgrade_store


@pytest.mark.parametrize("version", range(1105, 1119))
def test_every_old_format_preserves_truth_rules_entities_and_claim_index(app, version):
    core, ctx = app
    source = capture(core, ctx, "TEST-project theme amber.")
    claim = accept(core, ctx, draft(source, "amber", kind="fact", predicate="theme")).items[0]
    with closing(sqlite3.connect(core.storage.path)) as db:
        original = db.execute("SELECT event_id,content,content_sha256 FROM source_events").fetchall()
    downgrade_store(core.storage.path, version)
    with closing(sqlite3.connect(core.storage.path)) as db:
        if version >= 1113:
            db.execute(
                "INSERT INTO entity_names(audience,name,normalized,terms) VALUES "
                "('TEST-audience','TEST-preserved','test-preserved','test-preserved')"
            )
        if version >= 1114:
            db.execute(
                "INSERT INTO injection_rules VALUES "
                "('TEST-preserved-rule','instruction','regex','TEST preservedword','pending_review',0)"
            )
        db.commit()
    core.initialize()
    with closing(sqlite3.connect(core.storage.path)) as db:
        identity, born = db.execute("SELECT database_id,database_created_at FROM instance_meta").fetchone()
        assert len(identity) == 32 and born is None
        assert db.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert SCHEMA_VERSION == 1130
        assert db.execute("SELECT event_id,content,content_sha256 FROM source_events").fetchall() == original
        assert db.execute("SELECT current_revision FROM claims WHERE claim_id=?", (claim.ref,)).fetchone() == (1,)
        assert db.execute("SELECT 1 FROM claim_lexical_heads WHERE claim_id=?", (claim.ref,)).fetchone()
        assert db.execute("SELECT 1 FROM claim_lexical_postings WHERE claim_id=?", (claim.ref,)).fetchone()
        if version >= 1113:
            assert db.execute("SELECT name FROM entity_names WHERE name='TEST-preserved'").fetchone()
        if version >= 1114:
            assert db.execute(
                "SELECT pattern,enabled FROM injection_rules WHERE rule_id='TEST-preserved-rule'"
            ).fetchone() == ("TEST preservedword", 0)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    core.initialize()
    with core.storage.read(ctx) as tx:
        assert tx.database_identity() == (identity, None)


def test_content_import_preserves_destination_database_identity(app):
    core, ctx = app
    with core.storage.read(ctx) as tx:
        before = tx.database_identity()
    item = ImportRecord("TEST-import", "TEST imported amber document.", "document")
    saved = import_record(core, ctx, item, source="TEST-export", source_id="TEST-account", scope_id="TEST-scope")
    assert saved.event_refs
    with core.storage.read(ctx) as tx:
        assert tx.database_identity() == before
