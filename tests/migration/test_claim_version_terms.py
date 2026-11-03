"""All-version nomination upgrades preserve truth and recover from interrupted DDL."""

import sqlite3
from contextlib import closing

import pytest
from scope_recall.core import storage
from scope_recall.core.schema import SCHEMA_VERSION

from tests.contract.test_claim_lexical_index import search
from tests.contract.test_claims import app, initial
from tests.v11_support import downgrade_store


@pytest.mark.parametrize("version", range(1122, 1134))
@pytest.mark.parametrize("interrupted", [False, True])
def test_body_scan_upgrade_is_lazy_and_atomic(app, monkeypatch, version, interrupted):
    core, ctx = app
    item, source = initial(core, ctx)
    payload = core.current_claim(ctx, item.ref).payload
    downgrade_store(core.storage.path, version)
    step = storage.SCHEMA_MIGRATIONS[1133]
    if interrupted:

        def fail(conn):
            step(conn)
            raise RuntimeError("TEST interrupted body verdict DDL")

        monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1133, fail)
        with pytest.raises(RuntimeError, match="TEST interrupted body"):
            core.storage.initialize()
        with closing(sqlite3.connect(core.storage.path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == version
            assert "secret_scan_version" not in {row[1] for row in conn.execute("PRAGMA table_info(source_events)")}
        monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1133, step)
    core.storage.initialize()
    with core.storage.read(ctx) as tx:
        assert tx._check().execute("PRAGMA user_version").fetchone()[0] == 1134
        assert tx._check().execute("SELECT secret_scan_version,secret_scan_names FROM source_events").fetchone()[:] == (
            None,
            None,
        )
    assert core.source(ctx, source.ref, 1).event == source.event
    assert core.current_claim(ctx, item.ref).payload == payload


def test_profile_covering_index_is_installed(app):
    core, ctx = app
    with core.storage.read(ctx) as tx:
        row = tx._check().execute("SELECT sql FROM sqlite_master WHERE name='claim_profile_heads'").fetchone()
    assert row is not None
    assert "WHERE kind IN ('preference','constraint') AND read_blocked=0 AND suppressed=0" in row[0]


@pytest.mark.parametrize("version", range(1122, 1133))
@pytest.mark.parametrize("interrupted", [False, True])
def test_profile_upgrade_chain_rolls_back_without_changing_truth(app, monkeypatch, version, interrupted):
    core, ctx = app
    item, source = initial(core, ctx)
    downgrade_store(core.storage.path, version)
    step = storage.SCHEMA_MIGRATIONS[1132]
    if interrupted:

        def fail(conn):
            step(conn)
            raise RuntimeError("TEST interrupted profile index")

        monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1132, fail)
        with pytest.raises(RuntimeError, match="TEST interrupted profile"):
            core.storage.initialize()
        with closing(sqlite3.connect(core.storage.path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == version
            assert conn.execute("SELECT schema_version FROM instance_meta").fetchone()[0] == version
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='claim_profile_heads'").fetchone() is None
        monkeypatch.setitem(storage.SCHEMA_MIGRATIONS, 1132, step)
    core.storage.initialize()
    assert core.source(ctx, source.ref, 1).event == source.event
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "蓝色"
    test_profile_covering_index_is_installed((core, ctx))


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
