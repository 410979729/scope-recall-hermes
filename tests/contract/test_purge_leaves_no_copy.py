"""What a forget leaves in the files of the store: the bytes on disk, not the rows a query returns."""

import sqlite3
from contextlib import closing

from test_claims import app
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


def test_a_stored_capture_leaves_no_copy_of_its_inbox_row(app):
    """A host's capture waits in the inbox until it is stored, and its row goes when it is: what the row held is
    overwritten with it, so the store's files hold the words once, in the stored message."""
    core, ctx = app
    host_capture(core, ctx, "TEST-inbox/1", SECRET)
    with closing(sqlite3.connect(core.storage.path)) as conn:
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    assert copies(ctx.binding.data_directory, SECRET) == {"memory.sqlite3": 1}
