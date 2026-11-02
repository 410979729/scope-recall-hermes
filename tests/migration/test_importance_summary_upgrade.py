"""Every supported pre-summary store keeps truth while gaining salience and summaries."""

import sqlite3
from contextlib import closing

import pytest
from scope_recall.core.importance import backfill_importance
from scope_recall.core.schema import SCHEMA_VERSION, UPGRADE_CHAIN

from tests.contract.test_claims import app, initial
from tests.v11_support import downgrade_store


def test_injection_delivery_upgrade_preserves_existing_marks():
    """Existing briefing deliveries remain identifiable after a schema upgrade."""
    from scope_recall.core.schema import SESSION_BRIEF_STATEMENTS, upgrade_1128

    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE instance_meta(singleton INTEGER PRIMARY KEY, schema_version INTEGER)")
        db.execute("INSERT INTO instance_meta VALUES(1,1128)")
        db.execute(SESSION_BRIEF_STATEMENTS[2])
        db.execute(
            "INSERT INTO session_brief_injections(entry_id,session_id,object_kind,object_ref,revision,delivered_at) "
            "VALUES('TEST-entry','TEST-session','claim','TEST-ref',1,'2026-01-01')"
        )
        upgrade_1128(db)
        assert db.execute("PRAGMA user_version").fetchone() == (1129,)
        assert db.execute("SELECT object_ref,source FROM session_brief_injections").fetchall() == [
            ("TEST-ref", "brief")
        ]


def test_injection_delivery_upgrade_rolls_back_on_failure(app, monkeypatch):
    """An interrupted delivery schema upgrade leaves the old layout readable."""
    from scope_recall.core import storage

    core, ctx = app
    initial(core, ctx)
    downgrade_store(core.storage.path, 1128)
    step = storage.SCHEMA_MIGRATIONS[1128]

    def fail(db):
        step(db)
        raise RuntimeError("TEST interrupted delivery migration")

    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1128, fail)
    with pytest.raises(RuntimeError, match="TEST interrupted"):
        core.initialize()
    with sqlite3.connect(core.storage.path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1128,)
        columns = {row[1] for row in db.execute("PRAGMA table_info(session_brief_injections)")}
        assert "source" not in columns and "notice_marker" not in columns
        assert db.execute("SELECT count(*) FROM claims").fetchone() == (1,)


@pytest.mark.parametrize("version", range(1105, 1129))
def test_all_prior_schemas_upgrade_with_hashes_safety_index_and_defaults(app, version):
    core, ctx = app
    item, source = initial(core, ctx)
    with closing(sqlite3.connect(core.storage.path)) as db:
        before = db.execute("SELECT event_id,content,content_sha256 FROM source_events").fetchall()
        identity = db.execute("SELECT database_id FROM instance_meta").fetchone()[0]
    downgrade_store(core.storage.path, version)
    with closing(sqlite3.connect(core.storage.path)) as db:
        if version >= 1114:
            db.execute(
                "INSERT INTO injection_rules VALUES "
                "('TEST-summary-rule','instruction','regex','TEST protected','pending_review',0)"
            )
        db.commit()
    assert core.initialize().schema_version == SCHEMA_VERSION == 1130
    assert UPGRADE_CHAIN == tuple(range(1105, SCHEMA_VERSION))
    with closing(sqlite3.connect(core.storage.path)) as db:
        assert db.execute("SELECT event_id,content,content_sha256 FROM source_events").fetchall() == before
        assert db.execute(
            "SELECT importance,pinned,importance_assigned FROM claims WHERE claim_id=?", (item.ref,)
        ).fetchone() == (3, 0, 0)
        assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {
            "summary_groups",
            "summary_parts",
            "summary_backfill",
            "source_safety",
            "injection_rules",
            "claim_lexical_heads",
            "claim_lexical_postings",
        }
        assert db.execute("SELECT 1 FROM claim_lexical_postings WHERE claim_id=?", (item.ref,)).fetchone()
        if version >= 1114:
            assert db.execute(
                "SELECT pattern,enabled FROM injection_rules WHERE rule_id='TEST-summary-rule'"
            ).fetchone() == ("TEST protected", 0)
        if version == 1119:
            assert db.execute("SELECT database_id FROM instance_meta").fetchone()[0] == identity
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with core.storage.write(ctx) as tx:
        assert backfill_importance(tx) >= 1
        assert (
            tx._check().execute("SELECT importance_assigned FROM claims WHERE claim_id=?", (item.ref,)).fetchone()[0]
            == 1
        )
