"""Every supported pre-summary store keeps truth while gaining salience and summaries."""

import sqlite3
from contextlib import closing

import pytest
from scope_recall.core.importance import backfill_importance
from scope_recall.core.schema import SCHEMA_VERSION, UPGRADE_CHAIN

from tests.contract.test_claims import app, initial
from tests.v11_support import downgrade_store


@pytest.mark.parametrize("version", range(1105, 1120))
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
    assert core.initialize().schema_version == SCHEMA_VERSION == 1128
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
