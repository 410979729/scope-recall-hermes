"""Durable entry state and the owner-only transition boundary."""

import json
import os

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
    """Check source owners through the same entry fence used by publication."""
    return paused_since(tx, entry_fence(tx, sources))


def sources_paused_now(storage, context, sources, *, remaining_seconds):
    """Recheck source owners at dispatch, after earlier preparation has finished."""
    with storage.read(context, remaining_seconds=remaining_seconds) as tx:
        return sources_paused(tx, sources)


def entry_fence_for_keys(tx, keys):
    """Capture the current versions of entry owners in one transaction."""
    return {key: state_for(tx, tx.context, entry_id=key) for key in set(keys)}


def entry_fence(tx, sources):
    """Capture the current versions of the source owners in one transaction."""
    return entry_fence_for_keys(tx, (source.entry_id for source in sources if source is not None))


def context_fence(tx, context):
    """Capture the entry identity and version of a request, without inventing a worker identity."""
    key = entry_key(context)
    return {} if key is None else entry_fence_for_keys(tx, (key,))


def entry_changed(tx, fence):
    """Reject a paused or changed entry at a read or delivery boundary."""
    return paused_since(tx, fence) or any(
        state_for(tx, tx.context, entry_id=key) != value for key, value in fence.items()
    )


def process_account_id():
    """Return the operating system's identity for the current process, independent of environment variables."""
    if os.name != "nt":
        return str(os.getuid())
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    security.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    security.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    security.ConvertSidToStringSidW.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        result = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return result.value
        finally:
            kernel.LocalFree(result)
    finally:
        kernel.CloseHandle(token)


def history_actor(value):
    """Decode process identity while retaining older history records without one."""
    # Older labels were limited to 240 characters, so padded envelopes cannot
    # be mistaken for a label that happens to resemble the serialization.
    if len(value) > 240 and value.startswith("account-v1:"):
        try:
            data = json.loads(value[len("account-v1:") :])
            return data["label"], data["account_id"]
        except (ValueError, KeyError, TypeError):
            pass
    return value, None


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
        (
            entry_id,
            version,
            previous,
            state,
            ("account-v1:" + json.dumps({"label": actor.strip(), "account_id": process_account_id()})).ljust(241),
            now,
        ),
    )
    return {"id": entry_id, "state": state, "version": version}
