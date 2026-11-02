"""Claim-kind migration preserves history, derived postings and rollback snapshots."""

import sqlite3
from contextlib import closing

from scope_recall.core.schema import SCHEMA_VERSION

from tests.contract.test_claims import app, initial
from tests.v11_support import downgrade_store


def test_observation_migration_preserves_fact_versions_and_postings(app):
    core, ctx = app
    item, source = initial(core, ctx)
    with core.storage.read(ctx) as tx:
        payload = tx.claims.version(item.ref, item.revision).payload
        versions = [tuple(r) for r in tx._check().execute("SELECT * FROM claim_versions")]
        postings = [tuple(r) for r in tx._check().execute("SELECT * FROM claim_lexical_postings")]
    downgrade_store(core.storage.path, 1122)
    core.storage.initialize()
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [tuple(r) for r in tx._check().execute("SELECT * FROM claim_versions")] == versions
        assert [tuple(r) for r in tx._check().execute("SELECT * FROM claim_lexical_postings")] == postings
        assert not tx._check().execute("PRAGMA foreign_key_check").fetchall()
        assert tx.claims.version(item.ref, item.revision).payload == payload
        assert tx.source(source.ref, source.revision).event == source.event
    snapshots = list(core.storage.path.parent.glob("upgrade-snapshots/*.sqlite3"))
    with closing(sqlite3.connect(snapshots[0])) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1122


def test_interrupted_claim_kind_upgrade_rolls_back_the_entire_step(app, monkeypatch):
    import pytest
    from scope_recall.core import storage

    core, ctx = app
    initial(core, ctx)
    downgrade_store(core.storage.path, 1122)
    with closing(sqlite3.connect(core.storage.path)) as db:
        original_claims = db.execute("SELECT * FROM claim_versions").fetchall()
        original_triggers = db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        ).fetchall()
    migrate = storage.SCHEMA_MIGRATIONS[1122]

    def interrupted(db):
        migrate(db)
        raise RuntimeError("TEST interrupted observation migration")

    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1122, interrupted)
    with pytest.raises(RuntimeError, match="TEST interrupted"):
        core.initialize()
    with closing(sqlite3.connect(core.storage.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1122
        assert db.execute("SELECT * FROM claim_versions").fetchall() == original_claims
        assert (
            db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()
            == original_triggers
        )
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='observations'").fetchone() is None
