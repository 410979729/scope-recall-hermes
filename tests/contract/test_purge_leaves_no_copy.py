"""What a forget leaves in the files of the store: the bytes on disk, not the rows a query returns."""

import sqlite3
from contextlib import closing

from scope_recall.core.claims import Qualification
from scope_recall.core.events import lexical_terms
from scope_recall.runtime.lance_port import LanceIndexWriter, LancePurgePort, LanceVectorRecord
from scope_recall.vector.store import build_vector_store
from test_claims import app, capture, draft
from test_deletion import authorize, request
from v11_support import source_event

#: A message only these tests send, the words a claim quotes from it, and that claim's subject, predicate and value.
SECRET = "TEST 紫色仓库的钥匙藏在第七块砖下，暗号 violet-key-under-brick-seven。"
QUOTE = "钥匙藏在第七块砖下"
CLAIM = dict(subject="entity-violet-brick", predicate="hides-the-violet-key", value="under-brick-seven-today")
SPACE = "TEST-space"


def forgotten_fields():
    """Every field of the message the store keeps: its text, the quote, the claim's subject, predicate and value (the
    candidate's trigger terms are these), and each term the lexical index takes from the text but ``test``, which
    the store's schema spells as well."""
    return sorted({SECRET, QUOTE, *CLAIM.values(), *(term for term in lexical_terms(SECRET) if term != "test")})


def host_capture(core, ctx, key, text):
    """Store a message the way the hosts store one, through the capture inbox; the stored source."""
    receipt = core.record_host_event(
        ctx,
        source_event(source_event_key=key, content=text),
        scope_id="TEST-scope",
        host_scope=None,
        remaining_seconds=10,
    )
    assert receipt.durability == "persisted", receipt
    written = receipt.event_refs[0]
    return core.source(ctx, written.ref, written.revision)


def candidate_from(core, ctx, source):
    """``CLAIM``, proposed from ``source`` with ``QUOTE`` as its evidence and registered as a candidate."""
    proposal = draft(
        source,
        CLAIM["value"],
        quote_override=QUOTE,
        subject=CLAIM["subject"],
        predicate=CLAIM["predicate"],
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
    return candidate


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


def sqlite_history(core, ctx, deleted):
    """The ``sqlite_history`` layer of the delete's receipt."""
    with core.storage.read(ctx) as tx:
        return tx.deletions.receipt(deleted["operation_id"])["layers"]["sqlite_history"]


class Acknowledged:
    """A vector purge port with nothing to remove."""

    def purge_active(self, operation_id, *, receipt, remaining_seconds):
        return True


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
    candidate_from(core, ctx, source)
    triggers = "SELECT term FROM candidate_trigger_terms"
    assert first_column(core, triggers) == set(CLAIM.values())
    authorize(core, ctx, source)
    deleted = core.forget(ctx, request(source), remaining_seconds=10)
    core.operations.purge_sqlite(ctx, deleted["operation_id"], remaining_seconds=10)
    terms = first_column(core, "SELECT term FROM lexical_terms")
    assert first_column(core, triggers) == set()
    assert not {"紫色", "violet-key-under-brick-seven"} & terms
    assert {"钥匙", "test"} <= terms


def test_a_purged_message_leaves_no_copy_in_the_store_files(app):
    """After a forget and the worker's purge, no file in the store's directory holds a field of the message: not
    ``memory.sqlite3`` or its write-ahead log, nor the SQLite vector companion the purge removes its vector from.
    Another process holds the store open meanwhile, as a host does, so the log outlives the worker's connections."""
    core, ctx = app
    directory = ctx.binding.data_directory
    vectors = build_vector_store(
        "sqlite-bruteforce", storage_dir=directory / "vectors" / SPACE, table_name="TEST_vectors", dimensions=2
    )
    vectors.open()
    host = sqlite3.connect(f"{core.storage.path.as_uri()}?mode=ro", uri=True)
    try:
        host.execute("SELECT count(*) FROM sqlite_master").fetchall()
        source = host_capture(core, ctx, "TEST-purge/1", SECRET)
        binding = ctx.binding
        record = LanceVectorRecord(
            "event",
            source.ref,
            source.revision,
            f"TEST:{source.ref}",
            SPACE,
            (0.25, 0.75),
            "TEST-scope",
            binding.agent_id,
            binding.installation_id,
            ctx.project_id,
            ctx.branch_id,
        )
        assert LanceIndexWriter(vectors).upsert_fenced(record, guard=lambda: True, remaining_seconds=5)
        candidate_from(core, ctx, source)
        host_capture(core, ctx, "TEST-purge/2", f"删除 {source.ref}")
        deleted = core.forget(ctx, request(source), remaining_seconds=10)
        port = LancePurgePort(
            vectors, embedding_spaces=(SPACE,), agent_id=binding.agent_id, installation_id=binding.installation_id
        )
        passed = core.drain_worker(ctx, purge=port, remaining_seconds=30)
        assert [(item.work_type, item.disposition) for item in passed.items] == [("purge", "completed")]
        files = {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }
        assert {"memory.sqlite3", "memory.sqlite3-wal"} <= files.keys()
        held = {name: [field for field in forgotten_fields() if field.encode() in data] for name, data in files.items()}
        assert held == {name: [] for name in files}
        assert not set(forgotten_fields()) & first_column(core, "SELECT term FROM lexical_terms")
        assert first_column(core, "SELECT term FROM candidate_trigger_terms") == set()
        assert vectors.count_rows() == 0 and sqlite_history(core, ctx, deleted) == "removed"
    finally:
        host.close()
        vectors.close()


def test_a_reader_on_an_older_snapshot_keeps_the_log_until_a_later_pass(app):
    """A reader still on a snapshot from before the purge keeps the write-ahead log from being truncated: the worker's
    pass gives up on it and goes on, the receipt says the SQLite history is still pending, and the purge stays queued.
    The next pass after the reader is done truncates the log and completes the purge."""
    core, ctx = app
    source = host_capture(core, ctx, "TEST-busy/1", SECRET)
    host_capture(core, ctx, "TEST-busy/2", f"删除 {source.ref}")
    deleted = core.forget(ctx, request(source), remaining_seconds=10)
    reader = sqlite3.connect(f"{core.storage.path.as_uri()}?mode=ro", uri=True, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM source_events").fetchall()
        first = core.drain_worker(ctx, purge=Acknowledged(), remaining_seconds=30)
        assert [(item.work_type, item.disposition, item.state) for item in first.items] == [
            ("purge", "retry", "pending")
        ]
        assert sqlite_history(core, ctx, deleted) == "pending"
    finally:
        reader.close()
    core.clock.now = "2026-09-06T13:00:00Z"
    second = core.drain_worker(ctx, purge=Acknowledged(), remaining_seconds=30)
    assert [(item.work_type, item.disposition) for item in second.items] == [("purge", "completed")]
    assert sqlite_history(core, ctx, deleted) == "removed"
