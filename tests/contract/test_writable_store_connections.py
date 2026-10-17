"""Every writable connection the package opens on a store holds the store's writer lease and overwrites with zeros
what it deletes: the core's, and the maintenance commands' that write to a store or lock it for writing."""

import sqlite3
import sys
from contextlib import closing

import pytest
from scope_recall.core import truth_connection
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.core.storage import SQLiteStorage
from scope_recall.maintenance import cli, install_purge, migration_activation
from v11_support import context

#: The connections a test opens itself, which the recording leaves out.
CONNECT = sqlite3.connect


@pytest.fixture
def closed(monkeypatch):
    """Each connection the package closes while the test runs, as ``(opener, database, secure_delete, leased)``."""
    found = []

    def connect(database, *args, factory=sqlite3.Connection, **kwargs):
        opener = sys._getframe(1).f_code.co_name

        class Recorded(factory):
            def close(self):
                if not getattr(self, "recorded", False):
                    self.recorded = True
                    secure = self.execute("PRAGMA secure_delete").fetchone()[0]
                    leased = isinstance(self, truth_connection._LeasedTruthConnection)
                    found.append((opener, str(database), secure, leased))
                super().close()

        return CONNECT(database, *args, factory=Recorded, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    return found


def test_every_writable_connection_on_the_store_holds_the_lease_and_deletes_securely(tmp_path, closed):
    """The core's transactions, the header restamp after a 2.0 process stamped its own, the uninstall purge's look at
    what the store lists, and the checkpoint that seals a migration all open the store through the truth boundary."""
    ctx = context(tmp_path / "TEST-store")
    storage = SQLiteStorage(ctx.binding)
    storage.initialize()
    with storage.write(ctx) as tx:
        tx.status()
    with closing(CONNECT(storage.path)) as conn:
        conn.execute("PRAGMA user_version=10815")
        conn.commit()
    assert cli._restamp_header(storage.path, SCHEMA_VERSION, timeout=1.0)
    binding = ctx.binding
    assert (
        install_purge._expected_retained(
            storage.path,
            binding.data_directory,
            installation_id=binding.installation_id,
            agent_id=binding.agent_id,
        )
        == set()
    )
    migration_activation._checkpoint_existing_memory_db(binding.data_directory)
    writable = [entry for entry in closed if "memory.sqlite3" in entry[1] and "mode=ro" not in entry[1]]
    assert {opener for opener, *_rest in writable} == {"connect_truth_database"}
    assert [entry for entry in writable if entry[2:] != (1, True)] == []
