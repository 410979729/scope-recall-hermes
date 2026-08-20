"""Concrete MemoryCommandPort. Distinct from Provider and the query adapter."""

from __future__ import annotations

from typing import Any, Mapping

from .ports import MemoryCommandPort


def _call(host: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    fn = getattr(host, name, None)
    if not callable(fn):
        raise AttributeError(name)
    return fn(*args, **kwargs)


def _optional_mapping(host: Any, name: str) -> dict[str, Any]:
    fn = getattr(host, name, None)
    if not callable(fn):
        return {}
    payload = fn()
    return dict(payload) if isinstance(payload, Mapping) else {}


class ProviderCommandAdapter:
    """Command-port identity for RuntimeComposition.command_port.

    This object is not the Hermes Provider, not the query adapter, and not the
    tool port. CommandKernel talks to declared methods here. Domain modules that
    still need the Provider-shaped object use write_target(). Command methods
    forward to the host public wrappers so CommandKernel never has to unwrap.
    """

    def __init__(self, host: Any) -> None:
        self._host = host

    def write_target(self) -> object:
        return self._host

    def query_connection(self) -> Any:
        return _call(self._host, "query_connection")

    def query_lock(self) -> Any:
        return _call(self._host, "query_lock")

    def rollback_conn_after_error(self, context: str) -> None:
        _call(self._host, "rollback_conn_after_error", context)

    def store_now(
        self,
        *,
        content: str,
        source: str,
        target: str,
        session_id: str,
        metadata: dict[str, object] | None = None,
        allow_duplicate: bool = False,
        semantic_merge: bool = False,
        scope_mode: str | None = None,
    ) -> tuple[str, bool, str]:
        return _call(
            self._host,
            "store_now",
            content=content,
            source=source,
            target=target,
            session_id=session_id,
            metadata=metadata,
            allow_duplicate=allow_duplicate,
            semantic_merge=semantic_merge,
            scope_mode=scope_mode,
        )

    def command_update_memory(
        self, memory_id: str, content: str, target: str | None = None
    ) -> tuple[bool, str, str]:
        return _call(self._host, "command_update_memory", memory_id, content, target)

    def command_merge_memories(
        self,
        target_id: str,
        source_ids: list[str],
        content: str | None = None,
        target: str | None = None,
    ) -> dict[str, object]:
        return _call(self._host, "command_merge_memories", target_id, source_ids, content, target)

    def command_archive_memories(
        self,
        ids: list[str],
        *,
        reason: str = "scope_recall_forget",
        actor: str = "scope_recall_forget",
        batch_id: str = "",
    ) -> dict[str, object]:
        return _call(
            self._host,
            "command_archive_memories",
            ids,
            reason=reason,
            actor=actor,
            batch_id=batch_id,
        )

    def command_delete_memories(self, ids: list[str]) -> int:
        return int(_call(self._host, "command_delete_memories", ids))

    def command_feedback_memory(
        self, *, memory_id: str, rating: str, note: str = ""
    ) -> dict[str, object]:
        return _call(
            self._host, "command_feedback_memory", memory_id=memory_id, rating=rating, note=note
        )

    def command_govern_memories(
        self, *, dry_run: bool = True, scope_only: bool = True
    ) -> dict[str, object]:
        return _call(self._host, "command_govern_memories", dry_run=dry_run, scope_only=scope_only)

    def command_dedupe_memories(
        self, *, dry_run: bool = True, scope_only: bool = True
    ) -> dict[str, object]:
        return _call(self._host, "command_dedupe_memories", dry_run=dry_run, scope_only=scope_only)

    def command_repair_vector(self) -> dict[str, object]:
        return _call(self._host, "command_repair_vector")

    def fact_owned_memory_ids(self, ids: list[str]) -> list[str]:
        return list(_call(self._host, "fact_owned_memory_ids", ids))

    def clean_text(self, text: object) -> str:
        return str(_call(self._host, "clean_text", text) or "")

    def config_view(self) -> dict[str, object]:
        return _optional_mapping(self._host, "config_view")

    def config_value(self, key: str, default: object = None) -> object:
        fn = getattr(self._host, "config_value", None)
        if callable(fn):
            return fn(key, default)
        return self.config_view().get(key, default)

    def query_scope_view(self) -> Mapping[str, Any]:
        return _optional_mapping(self._host, "query_scope_view")

    def scope_id(self) -> str:
        return str(self.query_scope_view().get("scope_id") or "")

    def shared_scope_id(self) -> str:
        return str(self.query_scope_view().get("shared_scope_id") or "")

    def shared_pool_scope_id(self) -> str:
        return str(self.query_scope_view().get("shared_pool_scope_id") or "")

    def writable_scope_ids(self) -> list[str]:
        return [
            str(item)
            for item in (self.query_scope_view().get("writable_scope_ids") or [])
            if str(item)
        ]

    def vector_status_view(self) -> Mapping[str, Any]:
        return _optional_mapping(self._host, "vector_status_view")

    def has_positive_write_authority(self) -> bool:
        return bool(_call(self._host, "has_positive_write_authority"))

    def writer_lifecycle_lock(self) -> object:
        return _call(self._host, "writer_lifecycle_lock")


def bind_provider_command_adapter(obj: Any) -> MemoryCommandPort:
    """Return the unique command adapter for a composition host."""

    if isinstance(obj, ProviderCommandAdapter):
        return obj
    return ProviderCommandAdapter(obj)
