"""Claim-kind migration preserves history, derived postings and rollback snapshots."""

import sqlite3
from contextlib import closing

from scope_recall.core.schema import SCHEMA_VERSION

from tests.contract.test_claims import app, initial
from tests.v11_support import downgrade_store


def test_instruction_upgrade_and_interrupted_rollback(app, monkeypatch):
    """An interrupted instruction-table upgrade leaves the prior format untouched."""
    import pytest
    from scope_recall.core import storage
    from scope_recall.core.management import MemoryManagement
    from scope_recall.core.restore import InstallationMaintenance

    core, ctx = app
    downgrade_store(core.storage.path, 1124)
    migrate = storage.SCHEMA_MIGRATIONS[1124]

    def interrupted(db):
        migrate(db)
        raise RuntimeError("TEST interrupted instruction migration")

    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1124, interrupted)
    with pytest.raises(RuntimeError, match="TEST interrupted"):
        core.initialize()
    with closing(sqlite3.connect(core.storage.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1124
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='scope_memory_instructions'").fetchone() is None
    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1124, migrate)
    core.initialize()
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    assert owner.set_memory_instruction("TEST-scope", "TEST decisions")["version"] == 1


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


def test_policy_upgrade_keeps_observation_dependencies_and_rolls_back(app, monkeypatch):
    from scope_recall.core import storage

    from tests.contract.test_reflection import ReflectionModel, observation, populate, run

    core, ctx = app
    populate(core, ctx)
    run(core, ctx, ReflectionModel())
    old = observation(core, ctx)
    with core.storage.read(ctx) as tx:
        history = [tuple(row) for row in tx._check().execute("SELECT * FROM observations")]
    downgrade_store(core.storage.path, 1123)
    migrate = storage.SCHEMA_MIGRATIONS[1123]

    def interrupted(db):
        migrate(db)
        raise RuntimeError("TEST interrupted reflection policy")

    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1123, interrupted)
    import pytest

    with pytest.raises(RuntimeError, match="TEST interrupted"):
        core.initialize()
    with closing(sqlite3.connect(core.storage.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1123
        assert db.execute("SELECT * FROM observations").fetchall() == history
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='reflection_daily_usage'").fetchone() is None
    monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1123, migrate)
    core.initialize()
    with core.storage.read(ctx) as tx:
        assert tx.claims.version(old.ref, old.revision).payload == old.payload
        assert [tuple(row) for row in tx._check().execute("SELECT * FROM observations")] == history
        assert not tx._check().execute("PRAGMA foreign_key_check").fetchall()
