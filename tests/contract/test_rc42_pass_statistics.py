"""What a pass pays to know its database is the right one.

A pass called ``status()`` twice: once for its receipt, and once only to prove the database it
opened was the bound one.  That second call ran six whole-store aggregates and a JSON scan of
every source -- measured at 4.4 seconds on an instance with 150,000 queued items -- and threw
the answer away.  Proving the binding is one indexed row; the scans belong to the diagnostics
that are about sources.
"""
from __future__ import annotations

import sqlite3

from test_v11_claims import app, capture  # noqa: F401  (fixtures)


def _sources(core, ctx, count, *, tag):
    made = [capture(core, ctx, f"TEST 统计开销第{index}条。", key=f"TEST-{tag}/{index}") for index in range(count)]
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    return made


def test_proving_the_bound_database_is_one_indexed_row(app):
    """What a pass pays to know its database is the right one.

    It used to call ``status()``, whose six whole-store aggregates and JSON scan of every
    source cost 4.4 seconds on a queue of 150,000 -- and then discarded the answer.
    """
    core, ctx = app
    _sources(core, ctx, 3, tag="statements")
    with core.storage.read(ctx) as tx:
        statements: list[str] = []
        tx._check().set_trace_callback(statements.append)
        assert tx.memory_epoch() >= 1
        assert len(statements) == 1 and "instance_meta" in statements[0], statements
        statements.clear()
        assert tx.status(include_admission=False).source_only_sources is None
        assert not any("json_extract" in statement for statement in statements), statements
    # The scan is still there for the diagnostics that are about sources.
    with core.storage.read(ctx) as tx:
        assert tx.status(include_admission=True).source_only_sources is not None
