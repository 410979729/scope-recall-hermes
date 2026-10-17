"""What a pass pays to know its database is the right one.

A pass calls ``status()`` once, for its receipt.  Proving that the database it opened is the
bound one is one indexed row, not a second call, which would run six whole-store aggregates and
a JSON scan of every source -- 4.4 seconds on a queue of 150,000 items -- and throw the answer
away.  The scans belong to the diagnostics that are about sources.
"""

from __future__ import annotations

import sqlite3

from test_claims import app, capture  # noqa: F401  (fixtures)


def _sources(core, ctx, count, *, tag):
    made = [capture(core, ctx, f"TEST 统计开销第{index}条。", key=f"TEST-{tag}/{index}") for index in range(count)]
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
        conn.commit()
    return made


def test_proving_the_bound_database_is_one_indexed_row(app):
    """What a pass pays to know its database is the right one.

    It does not call ``status()``, whose six whole-store aggregates and JSON scan of every
    source cost 4.4 seconds on a queue of 150,000 -- and then discard the answer.
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
