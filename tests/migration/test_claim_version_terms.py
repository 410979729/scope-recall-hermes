"""All-version nomination upgrades preserve truth and recover from interrupted DDL."""

import sqlite3
from contextlib import closing

import pytest
from scope_recall.core import storage
from scope_recall.core.schema import SCHEMA_VERSION

from tests.contract.test_claim_lexical_index import search
from tests.contract.test_claims import app, initial
from tests.v11_support import downgrade_store


@pytest.mark.parametrize("version", range(1122, 1132))
@pytest.mark.parametrize("interrupted", [False, True])
def test_version_term_upgrade_is_atomic_and_preserves_nomination(app, monkeypatch, version, interrupted):
    core, ctx = app
    item, source = initial(core, ctx)
    downgrade_store(core.storage.path, version)
    step = storage.SCHEMA_MIGRATIONS[1131]
    if interrupted:

        def fail(conn):
            step(conn)
            raise RuntimeError("TEST interrupted version terms")

        monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1131, fail)
        with pytest.raises(RuntimeError, match="TEST interrupted"):
            core.storage.initialize()
        with closing(sqlite3.connect(core.storage.path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == version
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='claim_version_postings'").fetchone() is None
        monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1131, step)
    core.storage.initialize()
    assert core.source(ctx, source.ref, 1).event == source.event
    with core.storage.read(ctx) as tx:
        conn = tx._check()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert not conn.execute("SELECT 1 FROM claim_lexical_dirty").fetchone()
        rows = core.recall_pipeline.storage_reader.claims(tx, search(core, ctx, "TEST-project"), limit=6)
        assert [candidate.ref for candidate in rows] == [item.ref]


def test_version_term_projection_resumes_after_a_committed_page(app, monkeypatch):
    from scope_recall.core import lexical_index

    from tests.contract.test_claim_lexical_index import _synthetic_claims

    core, ctx = app
    _synthetic_claims(core, ctx, [(f"claim-TEST-resume-{i}", f"alpha{i}", "beta", "amber") for i in range(4)])
    downgrade_store(core.storage.path, 1131)
    native = lexical_index.backfill_claim_page
    pages = []

    def interrupt(conn, **kwargs):
        pages.append(1)
        if len(pages) == 2:
            raise InterruptedError("TEST interrupted projection page")
        return native(conn, limit=1)

    with monkeypatch.context() as patch:
        patch.setattr(lexical_index, "backfill_claim_page", interrupt)
        with pytest.raises(InterruptedError, match="TEST interrupted projection"):
            core.storage.initialize()
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("SELECT count(DISTINCT claim_id) FROM claim_version_postings").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM claim_lexical_dirty").fetchone()[0] == 4
        assert (
            conn.execute("SELECT completed FROM candidate_scan_cursors WHERE cursor_name='claim_lexical'").fetchone()[0]
            == 0
        )
    core.storage.initialize()
    with core.storage.read(ctx) as tx:
        conn = tx._check()
        assert conn.execute("SELECT count(DISTINCT claim_id) FROM claim_version_postings").fetchone()[0] == 5
        assert not conn.execute("SELECT 1 FROM claim_lexical_dirty").fetchone()
