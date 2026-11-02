"""Capture retry retains supported privacy decisions while ordinary writes reject an upgrade."""

import sqlite3
from contextlib import closing

import pytest
from scope_recall.contracts import ContractError
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance
from scope_recall.core.schema import SCHEMA_VERSION


@pytest.mark.parametrize("private", [False, True])
def test_newer_store_preserves_hermes_capture_without_writing(private, adapter, monkeypatch):
    provider, _clock = adapter
    identity = provider._require_identity()
    core = provider._require_core()
    context = identity.trusted_context()
    owner = MemoryManagement(core, InstallationMaintenance(context))
    if private:
        owner.private_on(target="global")
    monkeypatch.setattr(provider._retry, "start", lambda: None)
    path = core.storage.path
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        db.execute("UPDATE instance_meta SET schema_version=?", (SCHEMA_VERSION + 1,))
        db.commit()
    with pytest.raises(ContractError, match="SCHEMA_UNSUPPORTED"):
        with core.storage.read(context):
            pass
    provider.sync_turn("TEST retained during upgrade", "TEST synthetic reply", session_id=identity.session_id)
    assert bool(provider._retry.captures) is (not private)
    with core.storage.capture_privacy(context) as tx:
        with pytest.raises(sqlite3.DatabaseError):
            tx._check().execute("SELECT content FROM source_events").fetchall()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 0
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.execute("UPDATE instance_meta SET schema_version=?", (SCHEMA_VERSION,))
        db.commit()
    provider._retry.write_buffered(seconds=5)
    assert not provider._retry.captures
    with closing(sqlite3.connect(path)) as db:
        assert bool(db.execute("SELECT count(*) FROM source_events").fetchone()[0]) is (not private)


def test_changed_privacy_contract_still_refuses_local_copy(adapter):
    provider, _clock = adapter
    identity = provider._require_identity()
    core = provider._require_core()
    with closing(sqlite3.connect(core.storage.path)) as db:
        db.execute("DROP INDEX private_target")
        db.commit()
    with pytest.raises(ContractError, match="privacy_contract"):
        core.private_capture_guard(identity.trusted_context())


@pytest.mark.parametrize("literal", ["full", "global"])
def test_privacy_contract_keeps_literal_case_significant(adapter, literal):
    from scope_recall.core.schema import PRIVATE_INTERVALS_STATEMENT, PRIVATE_TARGET_INDEX

    provider, _clock = adapter
    core = provider._require_core()
    with closing(sqlite3.connect(core.storage.path)) as db:
        db.execute("DROP TABLE private_intervals")
        db.execute(PRIVATE_INTERVALS_STATEMENT.replace("'" + literal + "'", "'" + literal.upper() + "'"))
        db.execute(PRIVATE_TARGET_INDEX)
        db.commit()
    with pytest.raises(ContractError, match="privacy_contract"):
        core.private_capture_guard(provider._require_identity().trusted_context())
