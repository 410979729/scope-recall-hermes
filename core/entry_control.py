"""Durable entry state and the owner-only transition boundary."""

from ..contracts import ContractError


def entry_key(context):
    """Resolve a host identity; a shared worker without an entry has no entry state."""
    if context.binding.installation_kind == "shared":
        return context.entry_id
    return "local"


def state_for(tx, context, *, entry_id=None):
    """Read current state and version in the caller's SQLite snapshot."""
    key = entry_id if entry_id is not None else entry_key(context)
    if key is None:
        return "normal", 0
    row = tx._check().execute("SELECT state,version FROM entry_controls WHERE entry_id=?", (key,)).fetchone()
    return (row[0], row[1]) if row else ("normal", 0)


def capture_refusal(tx, context, *, after_version=None):
    """Return a distinct reason for content forbidden by its entry's current state."""
    state, _version = state_for(tx, context)
    if state != "normal":
        return f"entry_state:{state}"
    key = entry_key(context)
    if key is not None and after_version is not None:
        row = (
            tx._check()
            .execute(
                "SELECT to_state FROM entry_control_history WHERE entry_id=? AND version>? "
                "AND to_state!='normal' ORDER BY version LIMIT 1",
                (key, after_version),
            )
            .fetchone()
        )
        if row is not None:
            return f"entry_state:{row[0]}"
    return None


def sources_paused(tx, sources):
    """Check the owning entries of sources, including work done by a shared worker."""
    return any(
        state_for(tx, tx.context, entry_id=key)[0] == "paused" for key in {source.entry_id for source in sources}
    )


def sources_paused_now(storage, context, sources, *, remaining_seconds):
    """Recheck source owners at dispatch, after earlier preparation has finished."""
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        return sources_paused(tx, sources)


def entry_fence_for_keys(tx, keys):
    """Capture the current versions of entry owners in one transaction."""
    return {key: state_for(tx, tx.context, entry_id=key) for key in set(keys)}


def entry_fence(tx, sources):
    """Capture the current versions of the source owners in one transaction."""
    return entry_fence_for_keys(tx, (source.entry_id for source in sources))


def paused_since(tx, fence):
    """Block a result when an owner is paused or was paused after its snapshot."""
    for key, (state, version) in fence.items():
        if state == "paused" or state_for(tx, tx.context, entry_id=key)[0] == "paused":
            return True
        if (
            key is not None
            and tx._check()
            .execute(
                "SELECT 1 FROM entry_control_history WHERE entry_id=? AND version>? AND to_state='paused' LIMIT 1",
                (key, version),
            )
            .fetchone()
        ):
            return True
    return False


def change(tx, context, entry_id, state, actor, now):
    """Atomically change one registered entry and append its operator audit record."""
    if state not in {"normal", "read-only", "paused"}:
        raise ContractError("INPUT_INVALID", "entry_state")
    if type(actor) is not str or not actor.strip() or len(actor) > 240:
        raise ContractError("INPUT_INVALID", "actor")
    if context.binding.installation_kind == "shared":
        if entry_id not in tx.registry.entries():
            raise ContractError("SOURCE_MISSING", "entry")
    elif entry_id != "local":
        raise ContractError("SOURCE_MISSING", "entry")
    conn = tx._check(write=True)
    previous, version = state_for(tx, context, entry_id=entry_id)
    if state == previous:
        return {"id": entry_id, "state": state, "version": version}
    version += 1
    conn.execute(
        "INSERT INTO entry_controls(entry_id,state,version) VALUES (?,?,?) "
        "ON CONFLICT(entry_id) DO UPDATE SET state=excluded.state,version=excluded.version",
        (entry_id, state, version),
    )
    conn.execute(
        "INSERT INTO entry_control_history VALUES (?,?,?,?,?,?)",
        (entry_id, version, previous, state, actor.strip(), now),
    )
    return {"id": entry_id, "state": state, "version": version}
