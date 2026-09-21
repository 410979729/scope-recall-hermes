"""Every companion store can be asked what the runtime asks of a store.

The runtime reaches a vector store through two seams: ``LanceIndexWriter`` publishes through
``fenced_upsert_records(rows, guard=, remaining_seconds=)``, and ``LancePurgePort`` purges through
``purge_governed_members(..., remaining_seconds=)``.  ``build_vector_store`` picks the helper-process
store on Windows and the in-process one everywhere else, and every fence and purge test drove the
helper-process store, on Windows.  So three gaps shipped unseen:

- the in-process LanceDB store had no ``fenced_upsert_records``: on Linux every publication failed with
  ``fenced_upsert_unsupported`` and the index stayed empty (#99, reported from a production install);
- its ``purge_governed_members`` took ``budget_seconds``, the name the Windows helper uses, and refused
  the port's ``remaining_seconds`` with a ``TypeError`` the port reads as "not purged": a forget never
  finished there;
- the SQLite companion, publishable since #85, had no purge at all.

The first test asks each store class for the two calls by signature.  It needs no native code, so it
runs on every platform and in this tier, which forbids the child process and the socket LanceDB needs
to load.  The behaviour is written once, as the three ``check_*`` functions below: this file runs them
against the SQLite companion, and ``tests/storage_native/test_in_process_lance_store.py`` runs them
against the in-process LanceDB store in the native tier.
"""
from __future__ import annotations

import inspect

import pytest

from scope_recall.adapters.lance import LanceIndexWriter, LancePurgePort, LanceVectorRecord
from scope_recall.vector.process_store import ProcessLanceVectorStore
from scope_recall.vector.sqlite_store import SQLiteBruteForceVectorStore
from scope_recall.vector.store import LanceVectorStore, build_vector_store

SPACE, SCOPE, AGENT, INSTALLATION = "TEST-space", "TEST-scope", "TEST-agent", "TEST-installation"


def _record(ref: str, vector: tuple[float, ...] = (0.25, 0.75), *, revision: int = 1, scope: str = SCOPE,
            vector_id: str | None = None) -> LanceVectorRecord:
    return LanceVectorRecord("event", ref, revision, vector_id or f"TEST:{ref}:{revision}", SPACE, vector, scope,
                             AGENT, INSTALLATION)


def _receipt(*refs: str, scope: str = SCOPE) -> dict:
    return {"physical_members": [{"kind": "event", "ref": ref} for ref in refs], "scope_ids": [scope],
            "project_id": None, "branch_id": None}


def _port(store) -> LancePurgePort:
    return LancePurgePort(store, embedding_spaces=[SPACE], agent_id=AGENT, installation_id=INSTALLATION)


@pytest.mark.parametrize("store_class", [LanceVectorStore, ProcessLanceVectorStore, SQLiteBruteForceVectorStore])
def test_each_store_class_takes_the_two_calls_the_runtime_makes(store_class):
    fenced = inspect.signature(store_class.fenced_upsert_records)
    fenced.bind(None, [], guard=lambda: True, remaining_seconds=1.0)
    purge = inspect.signature(store_class.purge_governed_members)
    purge.bind(None, members=[], agent_id=AGENT, installation_id=INSTALLATION, partitions=[],
               project_id=None, branch_id=None, remaining_seconds=1.0)


@pytest.mark.parametrize("backend", ["lancedb", "sqlite-bruteforce"])
def test_the_store_chosen_for_this_platform_has_both(backend, tmp_path):
    store = build_vector_store(backend, storage_dir=tmp_path, table_name="TEST_vectors", dimensions=2)
    assert callable(getattr(store, "fenced_upsert_records", None)), type(store).__name__
    assert callable(getattr(store, "purge_governed_members", None)), type(store).__name__


# -- the behaviour, for whichever in-process store is handed in ------------------------------------

def check_a_group_is_published_under_one_guard_check_and_a_refusal_writes_nothing(store) -> None:
    checks: list[bool] = []
    writer = LanceIndexWriter(store)
    assert writer.upsert_fenced_many([_record("event-1"), _record("event-2", (0.5, 0.5))],
                                     guard=lambda: not checks.append(True), remaining_seconds=5.0) is True
    assert sorted(store.list_ids()) == ["TEST:event-1:1", "TEST:event-2:1"] and checks == [True]
    assert writer.upsert_fenced(_record("event-3"), guard=lambda: False, remaining_seconds=5.0) is False
    assert "TEST:event-3:1" not in store.list_ids()


def check_a_forget_removes_every_revision_of_its_members_and_only_those(store) -> None:
    published = [_record("event-1"), _record("event-1", (0.3, 0.7), revision=2), _record("event-2", (0.5, 0.5)),
                 _record("event-1", scope="TEST-other-scope", vector_id="TEST:other-scope:event-1:1")]
    assert LanceIndexWriter(store).upsert_fenced_many(published, guard=lambda: True, remaining_seconds=5.0) is True

    assert _port(store).purge_active("TEST-operation", receipt=_receipt("event-1"), remaining_seconds=5.0) is True

    assert sorted(store.list_ids()) == ["TEST:event-2:1", "TEST:other-scope:event-1:1"], \
        "both revisions of the member are gone; another member, and the same ref in another scope, are not this forget's"
    assert _port(store).purge_active("TEST-operation", receipt=_receipt("event-1"), remaining_seconds=5.0) is True, \
        "an inventory verified empty is acknowledged"


def check_a_row_that_cannot_be_classified_is_never_acknowledged_as_gone(store) -> None:
    store.upsert_records([dict(id="TEST:stray", scope_id="TEST-partition", source="event-9", target="not json",
                               content="", summary="", updated_at="", vector=[0.1, 0.9])])

    assert _port(store).purge_active("TEST-operation", receipt=_receipt("event-1"), remaining_seconds=5.0) is False
    assert store.list_ids() == ["TEST:stray"], "and nothing is deleted on a guess"


CHECKS = (
    check_a_group_is_published_under_one_guard_check_and_a_refusal_writes_nothing,
    check_a_forget_removes_every_revision_of_its_members_and_only_those,
    check_a_row_that_cannot_be_classified_is_never_acknowledged_as_gone,
)


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__.removeprefix("check_"))
def test_the_sqlite_companion(check, tmp_path):
    store = build_vector_store("sqlite-bruteforce", storage_dir=tmp_path, table_name="TEST_vectors", dimensions=2, metric="cosine")
    store.open()
    try:
        check(store)
    finally:
        store.close()
