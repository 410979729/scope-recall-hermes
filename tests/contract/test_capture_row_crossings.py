"""What one capture's write hands across the Python/SQLite boundary: a count that does not grow with the text, the
session's episode or the store's scopes.

Every statement and every row read in Python hands the GIL over and back.  In a Hermes gateway whose other threads were
busy, each handoff waited out their switch interval: a tool output of 51,283 characters (9,348 terms) held the shared
store's writer lease 42 s, every other entry's write failed meanwhile, and Hermes skipped the tool hook for a minute
(yuheng and tianshu, 2026-10-05).  The same write beside one busy thread took 408 s before and 7.9 s after.  The
counts are what a regression raises; the time itself depends on the machine.
"""

from dataclasses import replace
import sqlite3

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core.events import lexical_terms
from scope_recall.core.storage import SQLiteStorage
from scope_recall.core.visibility import allowed, allowed_refs

from test_r1_candidate_lifecycle import _candidate
from test_shared_store import shared, shared_context  # noqa: F401  (fixture)
from test_v11_claims import app, capture, initial  # noqa: F401  (fixtures)
from test_v11_deletion import authorize, request
from v11_support import source_event

#: What two captures on the same path may differ by.
_SLACK = 4


class Crossings:
    """Statements run and rows read in Python, on every connection the store opens."""

    def __init__(self, monkeypatch):
        self.statements = self.rows = 0
        opened = SQLiteStorage._open

        def _open(storage, *args, **kwargs):
            conn = opened(storage, *args, **kwargs)
            conn.set_trace_callback(self._statement)
            factory = conn.row_factory

            def counted(cursor, row):
                self.rows += 1
                return row if factory is None else factory(cursor, row)

            conn.row_factory = counted
            return conn

        monkeypatch.setattr(SQLiteStorage, "_open", _open)

    def _statement(self, _sql) -> None:
        self.statements += 1

    def during(self, action) -> tuple[int, int]:
        statements, rows = self.statements, self.rows
        action()
        return self.statements - statements, self.rows - rows


def _close(early: tuple[int, int], late: tuple[int, int]) -> bool:
    return late[0] - early[0] <= _SLACK and late[1] - early[1] <= _SLACK


def test_a_capture_s_crossings_do_not_grow_with_its_terms(app, monkeypatch):
    core, ctx = app
    capture(core, ctx, "TEST 第一句。")
    many = " ".join(f"ident{index:05d}" for index in range(3000))
    assert len(lexical_terms(many)) >= 3000
    crossings = Crossings(monkeypatch)
    few = crossings.during(lambda: capture(core, ctx, "TEST 第二句 ident1。", origin="tool_observation"))
    lots = crossings.during(lambda: capture(core, ctx, many, origin="tool_observation"))
    assert _close(few, lots), (few, lots)


def test_a_capture_s_crossings_do_not_grow_with_its_episode(app, monkeypatch):
    """An episode holds up to 200 sources; joining it read each member's lineage, state and time row by row."""
    core, ctx = app
    capture(core, ctx, "TEST 第一句。")
    crossings = Crossings(monkeypatch)
    early = crossings.during(lambda: capture(core, ctx, "TEST 第二句。"))
    for index in range(150):
        capture(core, ctx, f"TEST 第{index}条记录。")
    late = crossings.during(lambda: capture(core, ctx, "TEST 最后一句。"))
    assert _close(early, late), (early, late)
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(DISTINCT episode_id) FROM episode_events").fetchone()[0] == 1


def test_a_transaction_reads_the_store_s_scopes_in_one_row(shared, monkeypatch):
    storage, binding = shared
    ctx = shared_context(binding)

    def open_one():
        with storage.read(ctx):
            pass

    crossings = Crossings(monkeypatch)
    few = crossings.during(open_one)
    with storage.write(ctx) as tx:
        tx.register_scopes({f"TEST-g{index}" for index in range(500)})
    assert crossings.during(open_one) == few


def test_a_local_store_still_opens_only_for_exactly_its_scopes(app):
    core, ctx = app
    wider = replace(ctx.binding, scope_ids=ctx.binding.scope_ids | {"TEST-missing"})
    with pytest.raises(ContractError) as missing:
        with SQLiteStorage(wider).read(replace(ctx, binding=wider)):
            pass
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("INSERT INTO instance_scopes(scope_id) VALUES ('TEST-extra')")
    with pytest.raises(ContractError) as extra:
        with core.storage.read(ctx):
            pass
    assert {(missing.value.code, missing.value.field), (extra.value.code, extra.value.field)} == {
        ("IDENTITY_UNBOUND", "scope_binding")
    }


def test_a_source_restating_a_muted_claim_is_muted_and_one_that_does_not_is_not(app):
    """The muted claims are picked before the content is searched; which sources inherit stays as it was."""
    core, ctx = app
    item, _source = initial(core, ctx)
    authorize(core, ctx, item, mode="suppress")
    core.forget(ctx, request(item, mode="suppress"), remaining_seconds=10)
    assert capture(core, ctx, "我们再说一次 TEST-project 配色 蓝色。").suppressed
    assert not capture(core, replace(ctx, project_id="TEST-other"), "TEST-project 配色 蓝色。").suppressed
    # Last: it reads as a correction of the claim.
    assert not capture(core, ctx, "TEST-project 配色 换成了别的。").suppressed


def test_the_batch_check_admits_exactly_what_the_single_one_does(app):
    core, ctx = app
    blocks = {
        "TEST-open": None,
        "TEST-read-blocked": (1, 0, "TEST-scope"),
        "TEST-muted": (0, 1, "TEST-scope"),
        "TEST-elsewhere": (0, 0, "TEST-elsewhere"),
    }
    with sqlite3.connect(core.storage.path) as conn:
        for kind in ("event", "claim"):
            for ref, block in blocks.items():
                if block:
                    conn.execute(
                        "INSERT INTO object_blocks VALUES (?,?,?,NULL,NULL,?,?,'TEST-op')",
                        (kind, ref, block[2], block[0], block[1]),
                    )
        conn.execute(
            "INSERT INTO restored_absence_blocks VALUES ('claim','TEST-open','TEST-scope',NULL,NULL,?,'TEST')",
            ("0" * 64,),
        )
    with core.storage.read(ctx) as tx:
        for kind in ("event", "claim"):
            for automatic in (False, True):
                single = {ref for ref in blocks if allowed(tx, kind, ref, automatic=automatic)}
                assert allowed_refs(tx, kind, blocks, automatic=automatic) == single, (kind, automatic)
        assert {ref for ref in blocks if allowed(tx, "event", ref)} == {"TEST-open", "TEST-muted"}


def _assistant(core, ctx, text: str, key: str):
    """What the assistant said, as Hermes stores it after a turn: not first-hand."""
    saved = core.record_event(
        replace(ctx, actor_origin="assistant_visible"),
        source_event(source_event_key=key, origin="assistant_visible", role="assistant", content=text),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )
    assert saved.durability == "persisted"


def test_what_the_assistant_said_reads_the_candidates_it_shares_a_word_with_in_one_row(app, monkeypatch):
    """A source that is not first-hand had every reachable candidate sharing one of its words read row by row, until
    sixteen of them were restated: none is, here (review of 3.7.6)."""
    core, ctx = app
    for index in range(5):
        _candidate(core, ctx, value=f"蓝色{index}", key=f"TEST-c/{index}")
    crossings = Crossings(monkeypatch)
    few = crossings.during(lambda: _assistant(core, ctx, "我觉得蓝色不错。", "TEST-said/1"))
    for index in range(5, 65):
        _candidate(core, ctx, value=f"蓝色{index}", key=f"TEST-c/{index}")
    lots = crossings.during(lambda: _assistant(core, ctx, "我还是觉得蓝色更好看。", "TEST-said/2"))
    assert _close(few, lots), (few, lots)


def test_a_capture_s_words_never_take_a_parameter_each(app, monkeypatch):
    """Matching a source to the candidates bound one parameter per term: a part of 64,000 characters held more distinct
    terms than SQLite takes parameters (32,766 by default), and the whole capture failed as the store being unavailable,
    to be kept and retried for good (review of 3.7.6).  The limit is lowered here, whatever the build's is."""
    core, ctx = app
    opened = SQLiteStorage._open

    def _open(storage, *args, **kwargs):
        conn = opened(storage, *args, **kwargs)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        return conn

    monkeypatch.setattr(SQLiteStorage, "_open", _open)
    text = " ".join(f"ident{index:05d}" for index in range(2000))
    assert len(lexical_terms(text)) > 999
    assert capture(core, ctx, text) is not None
