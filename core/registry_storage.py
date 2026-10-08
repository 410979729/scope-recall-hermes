"""A shared store's registry (``Transaction.registry``): the scopes its entries bring and the entries themselves."""

from __future__ import annotations

from ..contracts import ENTRY_ID, MAX_SHARED_SCOPES, ContractError


class Registry:
    def __init__(self, transaction) -> None:
        self._tx = transaction

    def register_scopes(self, scope_ids) -> int:
        """Add the scopes an attaching entry brings to a shared store; returns how many were new.

        Refused on a local store, whose scope set is its binding.  The total is
        held to what one binding carries, so the shared worker, which binds every
        scope, can still be built after the entry attaches.
        """
        conn = self._tx._check(write=True)
        if self._tx.context.binding.installation_kind != "shared":
            raise ContractError("ACCESS_DENIED", "local_store")
        scope_ids = frozenset(scope_ids)
        if not scope_ids or any(type(s) is not str or not s.strip() or len(s) > 240 for s in scope_ids):
            raise ContractError("INPUT_INVALID", "scope_ids")
        existing = frozenset(r[0] for r in conn.execute("SELECT scope_id FROM instance_scopes"))
        if len(existing | scope_ids) > MAX_SHARED_SCOPES:
            raise ContractError("INPUT_INVALID", "scope_limit")
        added = sorted(scope_ids - existing)
        conn.executemany("INSERT INTO instance_scopes(scope_id) VALUES (?)", [(s,) for s in added])
        return len(added)

    def register_entry(self, entry_id: str, display_name: str, host: str, *, now: str) -> None:
        """Record an entry of a shared store, or rename it; ``first_seen`` survives a rename."""
        conn = self._tx._check(write=True)
        if self._tx.context.binding.installation_kind != "shared":
            raise ContractError("ACCESS_DENIED", "local_store")
        if type(entry_id) is not str or not ENTRY_ID.fullmatch(entry_id):
            raise ContractError("INPUT_INVALID", "entry_id")
        for value, field in ((display_name, "display_name"), (host, "host")):
            if type(value) is not str or not value.strip() or len(value) > 32:
                raise ContractError("INPUT_INVALID", field)
        if type(now) is not str or not now:
            raise ContractError("INPUT_INVALID", "now")
        conn.execute(
            """INSERT INTO entries(entry_id,display_name,host,first_seen,last_seen) VALUES (?,?,?,?,?)
            ON CONFLICT(entry_id) DO UPDATE SET display_name=excluded.display_name, host=excluded.host""",
            (entry_id, display_name, host, now, now),
        )
        self._tx._entries_changed()

    def entries(self) -> dict[str, dict[str, str]]:
        """Every entry this store has registered, by id; empty for a local store."""
        conn = self._tx._check()
        return {
            r["entry_id"]: {
                "name": r["display_name"],
                "host": r["host"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
            }
            for r in conn.execute("SELECT * FROM entries ORDER BY entry_id")
        }
