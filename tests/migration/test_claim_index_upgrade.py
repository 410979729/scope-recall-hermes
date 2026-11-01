"""Resumable current-fact projection pages preserve SQLite truth and rollback material."""

import sqlite3
from contextlib import closing

import pytest
from scope_recall.core import lexical_index
from scope_recall.core.schema import SCHEMA_VERSION
from scope_recall.runtime.store_maintenance import store_upgrade_pending

from tests.contract.test_claims import accept, app, capture, draft
from tests.v11_support import downgrade_store


def test_claim_index_upgrade_resumes_after_committed_page(app, monkeypatch):
    core, ctx = app
    refs = []
    for index in range(4):
        subject = f"TEST-upgrade-{index}"
        source = capture(core, ctx, f"{subject} theme amber.")
        refs.append(accept(core, ctx, draft(source, "amber", subject=subject, predicate="theme")).items[0].ref)
    truth = [core.current_claim(ctx, ref).payload for ref in refs]
    downgrade_store(core.storage.path, 1113)
    page = lexical_index.backfill_claim_page
    calls = []

    def interrupt(conn, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise InterruptedError("TEST interrupted before second page")
        return page(conn, limit=1)

    with monkeypatch.context() as patch:
        patch.setattr(lexical_index, "backfill_claim_page", interrupt)
        with pytest.raises(InterruptedError):
            core.storage.initialize()
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute(
            "SELECT processed_count,completed FROM candidate_scan_cursors WHERE cursor_name='claim_lexical'"
        ).fetchone() == (1, 0)
        assert conn.execute("SELECT count(DISTINCT claim_id) FROM claim_lexical_postings").fetchone()[0] == 1
    assert len(tuple(core.storage.path.parent.glob("upgrade-snapshots/*.sqlite3"))) == 1
    assert store_upgrade_pending(ctx.binding)
    core.storage.initialize()
    core.storage.initialize()
    assert not store_upgrade_pending(ctx.binding)
    with core.storage.read(ctx) as tx:
        assert not lexical_index.claim_backfill_pending(tx._check())
        assert tx._check().execute("SELECT count(DISTINCT claim_id) FROM claim_lexical_postings").fetchone()[0] == 4
        assert tx._check().execute("PRAGMA foreign_key_check").fetchall() == []
    assert [core.current_claim(ctx, ref).payload for ref in refs] == truth
