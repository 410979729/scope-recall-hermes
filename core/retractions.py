"""Scope-bound withdrawal epochs shared by all authority-release boundaries."""

SAFETY_RETRACTIONS = """CREATE TABLE IF NOT EXISTS safety_retractions (
    scope_id TEXT PRIMARY KEY NOT NULL, memory_epoch INTEGER NOT NULL CHECK(memory_epoch>=0)
) STRICT, WITHOUT ROWID"""


def record_safety_retraction(conn, scopes):
    """Record a new safety hold in the same write transaction as its verdict."""
    scopes = tuple(sorted(set(scopes)))
    if not scopes:
        return
    # A store already at this schema may predate the optional withdrawal index.
    conn.execute(SAFETY_RETRACTIONS)
    conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
    epoch = conn.execute("SELECT memory_epoch FROM instance_meta WHERE singleton=1").fetchone()[0]
    conn.executemany(
        "INSERT INTO safety_retractions(scope_id,memory_epoch) VALUES(?,?) "
        "ON CONFLICT(scope_id) DO UPDATE SET memory_epoch=excluded.memory_epoch",
        [(scope, epoch) for scope in scopes],
    )


def retraction_after(conn, scope_ids, epoch: int) -> bool:
    """Whether a deletion, suppression or safety hold withdrew a scope after an epoch."""
    scopes = sorted(set(scope_ids))
    if not scopes:
        return False
    marks = ",".join("?" for _ in scopes)
    if conn.execute(
        f"SELECT 1 FROM deletion_operations o WHERE o.memory_epoch>? "
        f"AND EXISTS(SELECT 1 FROM json_each(o.scope_ids_json) s WHERE s.value IN ({marks})) LIMIT 1",
        (epoch, *scopes),
    ).fetchone():
        return True
    # An older store at the same schema has no rows until its first new hold.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='safety_retractions'").fetchone():
        return False
    return (
        conn.execute(
            f"SELECT 1 FROM safety_retractions WHERE scope_id IN ({marks}) AND memory_epoch>? LIMIT 1",
            (*scopes, epoch),
        ).fetchone()
        is not None
    )
