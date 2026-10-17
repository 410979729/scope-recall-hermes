"""What a forget leaves in the files of the store: the bytes on disk, not the rows a query returns."""

import sqlite3
from contextlib import closing

from scope_recall.core.claims import Qualification
from test_claims import app, capture, draft
from test_deletion import authorize, request
from v11_support import source_event

#: A message only these tests send.
SECRET = "TEST 紫色仓库的钥匙藏在第七块砖下，口令 violet-key-under-brick-seven。"


def host_capture(core, ctx, key, text):
    """Store a message the way the hosts store one: through the capture inbox."""
    receipt = core.record_host_event(
        ctx,
        source_event(source_event_key=key, content=text),
        scope_id="TEST-scope",
        host_scope=None,
        remaining_seconds=10,
    )
    assert receipt.durability == "persisted", receipt
    return receipt.event_refs[0]


def copies(directory, needle):
    """How many times each file under ``directory`` holds ``needle`` as UTF-8, for each file that holds it."""
    found = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and (count := path.read_bytes().count(needle.encode())):
            found[path.relative_to(directory).as_posix()] = count
    return found


def first_column(core, sql):
    """The first column of the rows ``sql`` reads from the store, as a set."""
    with closing(sqlite3.connect(core.storage.path)) as conn:
        return {row[0] for row in conn.execute(sql)}


def test_a_stored_capture_leaves_no_copy_of_its_inbox_row(app):
    """A host's capture waits in the inbox until it is stored, and its row goes when it is: what the row held is
    overwritten with it, so the store's files hold the words once, in the stored message."""
    core, ctx = app
    host_capture(core, ctx, "TEST-inbox/1", SECRET)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    assert copies(ctx.binding.data_directory, SECRET) == {"memory.sqlite3": 1}


def test_a_purge_takes_the_terms_only_the_forgotten_message_held(app):
    """The lexical index's dictionary and a candidate's trigger terms hold words of the message they came from: a
    purge removes the terms nothing else holds and keeps one another message still holds."""
    core, ctx = app
    capture(core, ctx, "TEST 钥匙在门口。", key="TEST-terms/kept")
    source = capture(core, ctx, SECRET, key="TEST-terms/secret")
    proposal = draft(
        source,
        "violet-key-under-brick-seven",
        subject="entity-said",
        predicate="property-said",
        kind="fact",
        statement_kind="fact",
    )
    with core.storage.write(ctx) as tx:
        candidate = tx.claims.append(
            "TEST-scope",
            proposal,
            Qualification("proposed", "inferred_suggestion", "TEST_candidate"),
            recorded_at=core.clock.utc_now(),
        )
        tx.candidates.register(candidate.ref, candidate.revision, observed_at=core.clock.utc_now())
    triggers = "SELECT term FROM candidate_trigger_terms"
    assert first_column(core, triggers) == {"entity-said", "property-said", "violet-key-under-brick-seven"}
    authorize(core, ctx, source)
    deleted = core.forget(ctx, request(source), remaining_seconds=10)
    core.operations.purge_sqlite(ctx, deleted["operation_id"], remaining_seconds=10)
    terms = first_column(core, "SELECT term FROM lexical_terms")
    assert first_column(core, triggers) == set()
    assert not {"紫色", "violet-key-under-brick-seven"} & terms
    assert {"钥匙", "test"} <= terms
